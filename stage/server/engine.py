"""演出引擎：服务端唯一的"播放头"，也是舞台状态唯一的 source of truth。

一个 asyncio task 循环：取下一局 → 逐事件演出（adapt → apply 折进 Scene → publish 全量
状态 → 按时长停留）→ 幕间页 → 再取下一局。下一拍/跳局/调速都是改这个 task 看的状态位。

玩法无关：引擎只通过 `GamePlugin`（registry 按 meta.game 派发）把 events 翻成 directive，
再用 `server/plans/<game>.yaml` 编译出统一 PlayPlan。主持摆放、节奏配方、音效都在 DSL；
引擎从不 import 或 match 任何具体指令类型。加玩法即并列一个插件和一份 plan，引擎零分叉。

publish 是注入的回调（app 层把"当前完整状态"广播给所有页面）；任何状态变化（每一拍、
旋钮、队列）都调一次 publish——客户端是纯渲染函数，收到什么画什么。

节奏模型由 PlayPlan DSL 驱动：pace 0~1 线性缩放每字停顿与事件间气口。
有离线 TTS 时（tts/manifest.json），一拍时长取 max(文字估算, 音频时长+余量)，
保证音频永远放得完；文字打字机始终先于音频收尾。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from server import registry, replay
from server.games.base import AdaptContext, GamePlugin
from server.playplan import StageBeat, StagePlanSpec, compile_stage_beats, load_plan, plan_path
from server.protocol import (
    BaseScene,
    FeedItem,
    GameResult,
    Intermission,
)
from server.settings import StageSettings

_TICK = 0.2  # 状态位轮询粒度：跳局/下一拍的最大响应延迟
FEED_CAP = 5  # 服务端只留最近几拍（窗口旋钮 1~3 再从中裁剪下发）


class PlaybackGate(Protocol):
    async def first_frame_published(self, seq: int) -> float | None: ...
    async def before_intermission(self) -> None: ...


class NullPlaybackGate:
    async def first_frame_published(self, seq: int) -> float | None:
        return None

    async def before_intermission(self) -> None:
        pass


def _beat_audio_urls(beat: StageBeat) -> list[str]:
    urls = [track.audio_url for track in beat.tracks if track.audio_url]
    url = getattr(beat.directive, "audio_url", None)
    if url and not urls:
        urls.append(url)
    return urls


def _upcoming_audio(beats: list[StageBeat], i: int, factor: int) -> list[str]:
    """从第 i 拍之后顺取最多 factor 条待播音频 URL（跳过无声拍），交给页面提前预热——
    起拍那一刻音频已在缓存里，currentTime 从 0 即时出声，尾巴不再被下一拍顶掉时截断。"""
    urls: list[str] = []
    for beat in beats[i + 1:]:
        if len(urls) >= factor:  # 放循环头：factor=0（关掉预取）时直接不取
            break
        for url in _beat_audio_urls(beat):
            urls.append(url)
            if len(urls) >= factor:
                break
    return urls


class Engine:
    def __init__(
        self,
        settings: StageSettings,
        publish: Callable[[], Awaitable[None]],
        playback_gate: PlaybackGate | None = None,
    ) -> None:
        self._root = settings.replay_root
        self._publish = publish
        self._playback_gate = playback_gate or NullPlaybackGate()
        self._plugin: GamePlugin = registry.default_plugin()  # 待机底图用默认玩法；每局 _play 时按 meta.game 换
        self._plan: StagePlanSpec = load_plan(plan_path(self._plugin.id))
        self.scene: BaseScene = self._plugin.new_scene()
        self.queue: list[str] = []
        self.pace = settings.pace
        self.speed = settings.speed
        self.intermission_seconds = settings.intermission_seconds
        self.loop_pool = settings.loop_pool
        self.feed_window = settings.feed_window
        self._prefetch_factor = settings.prefetch_factor
        self.phase: str = "idle"
        self.replay_id: str | None = None  # 只进带 token 的 /api/state，不进公开频道
        self.prefetch: list[str] = []  # 当前拍之后待播的音频 URL，进 StateMsg 让页面提前预热
        self._seq = 0
        self._skip = False
        self._step = False  # 下一拍：提前结束本拍停留、推进到下一拍（区别于 _skip 的整局跳走）
        self._pool_pick: str | None = None  # 取局缓存：保证"下一局已就绪"的判断与实际取局一致
        self._durations: dict[str, float] = {}  # 当前局的音频时长表（audio_url → 秒）

    # ---------------------------------------------------------------- 对外状态

    def library(self) -> list[replay.LibraryItem]:
        """replay_root 下所有可播的局（边界过滤：对局跑完即可；TTS 柔性，详见 replay.is_broadcastable）。"""
        return replay.library(self._root)

    def library_ids(self) -> list[str]:
        return [item.id for item in self.library()]

    def public_scene(self) -> BaseScene:
        """下发给页面的 Scene：feed 按窗口旋钮裁剪（服务端硬上限只是内部护栏）。"""
        return self.scene.model_copy(update={"feed": self.scene.feed[-self.feed_window:]})

    # ---------------------------------------------------------------- 导播台控制

    async def skip(self) -> None:
        self._skip = True

    async def step(self) -> None:
        self._step = True

    async def set_knobs(self, *, pace: float | None = None, speed: float | None = None,
                        intermission_seconds: float | None = None,
                        loop_pool: bool | None = None, feed_window: int | None = None) -> None:
        if pace is not None:
            self.pace = pace
        if speed is not None:
            self.speed = speed
        if intermission_seconds is not None:
            self.intermission_seconds = intermission_seconds
        if loop_pool is not None:
            self.loop_pool = loop_pool
        if feed_window is not None:
            self.feed_window = feed_window
        await self._publish()

    async def set_queue(self, queue: list[str]) -> None:
        self.queue = queue

    # ---------------------------------------------------------------- 主循环

    async def run(self) -> None:
        """主循环。单局演出炸了（坏 replay/契约漂移）大声记日志、跳下一局——
        stage 是 7×24 无人值守直播层，一局坏片绝不许冻结整个频道。"""
        while True:
            replay_id = await self._acquire_next()
            try:
                last = await self._play(replay_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("演出 %s 中途炸了，跳到下一局", replay_id)
                last = None
            await self._intermission(last)

    async def _acquire_next(self) -> str:
        """取下一局：队列优先，空了看循环池，都没有就 idle 等导播台排片。"""
        while True:
            if self.queue:
                replay_id = self.queue.pop(0)
                self._pool_pick = None
                return replay_id
            pick = self._peek_pool()
            if pick is not None:
                self._pool_pick = None
                return pick
            if self.phase != "idle":
                self.phase = "idle"
                self.replay_id = None
                await self._publish()
            await asyncio.sleep(1.0)

    def _peek_pool(self) -> str | None:
        if not self.loop_pool:
            return None
        if self._pool_pick is None:
            library = self.library()
            if not library:
                return None
            self._pool_pick = random.choice(library).id
        return self._pool_pick

    async def _play(self, replay_id: str) -> GameResult | None:
        self.phase = "playing"
        self.replay_id = replay_id
        replay_dir = self._root / replay_id
        self._plugin = registry.plugin_for_replay(replay_dir)  # 按 meta.game 换玩法插件
        self._plan = load_plan(plan_path(self._plugin.id))
        self.scene = self._plugin.new_scene()  # 每局起一张该玩法的空白 Scene（类型随玩法变）
        tts = replay.load_tts(replay_dir)
        self._durations = replay.audio_durations(replay_id, tts)
        # 身份信息随各局 meta 自带：不同局可用不同人设/网名/头像/对外模型名（详见 server.replay）。
        ctx = AdaptContext(
            replay_id=replay_id, tts=tts,
            names=replay.load_names(replay_dir),
            host_name=replay.load_host_name(replay_dir),
            avatars=replay.avatar_urls(replay_id, replay_dir),
            model_labels=replay.load_model_labels(replay_dir),
        )
        # 先把整局摊平：前瞻要看后面的拍，懒迭代给不了"下一拍是谁"。一局事件量很小，全量无妨。
        # adapt 返回 None 表示"这条事件不演"（如主持人复述身份的 reveal 拍），过滤掉不进时间线。
        directives = [d for event in replay.load_events(replay_dir)
                      if (d := self._plugin.adapt(event, ctx)) is not None]
        beats = self._stage_beats(directives)
        last: GameResult | None = None
        for i, beat in enumerate(beats):
            directive = beat.directive
            if (result := self._plugin.result(directive)) is not None:
                last = result
            self.prefetch = _upcoming_audio(beats, i, self._prefetch_factor)
            await self._emit(beat)
            if self._plugin.is_opening_anchor(directive):
                started_at = await self._playback_gate.first_frame_published(self._seq)
                if started_at is not None:
                    self.scene.feed[-1].started_at = started_at
                    await self._publish()
            if not await self._dwell(beat.duration):
                break  # 导播台跳局
        self.prefetch = []
        return last

    async def _intermission(self, last: GameResult | None) -> None:
        await self._playback_gate.before_intermission()
        self.phase = "intermission"
        self.replay_id = None
        directive = Intermission(
            last_winner=last.winner if last else None,
            last_reason=last.win_reason if last else None,
            next_ready=bool(self.queue) or self._peek_pool() is not None,
        )
        await self._emit(StageBeat(directive, duration=self.intermission_seconds))
        await self._dwell(self.intermission_seconds)

    async def _emit(self, beat: StageBeat) -> None:
        self._seq += 1
        item = FeedItem(
            seq=self._seq,
            directive=beat.directive,
            started_at=time.time(),
            duration=beat.duration,
            speed=self.speed,
            content_at=beat.content_at,
            narrations=beat.narrations,
            tracks=beat.tracks,
        )
        # feed 入队 + 窗口护栏是玩法无关的，引擎统一做；玩法 reducer（plugin.apply）只折自己的状态。
        self.scene.feed.append(item)
        del self.scene.feed[:-FEED_CAP]
        self._plugin.apply(self.scene, item)
        await self._publish()

    async def _dwell(self, seconds: float) -> bool:
        """这一拍在台上停留：跳局立即返回 False（break 整局），下一拍提前结束返回 True（推进下一拍）。

        pace=0 时时长为 0，也必须让出一次事件循环——否则引擎独占线程，
        WS 广播、控制 API 全部饿死（实测会把内存吃爆）。"""
        await asyncio.sleep(0)
        remaining = seconds
        while remaining > 0:
            if self._skip:
                self._skip = False
                return False
            if self._step:
                self._step = False
                return True  # 本拍不等了，立刻推进到下一拍
            step = min(remaining, _TICK)
            await asyncio.sleep(step)
            remaining -= step
        return not self._consume_skip()

    def _consume_skip(self) -> bool:
        if self._skip:
            self._skip = False
            return True
        return False

    # ---------------------------------------------------------------- 节奏

    def _stage_beats(self, directives) -> list[StageBeat]:
        """Compile replay directives into the single play plan consumed by live and record."""
        return compile_stage_beats(directives, self._plan, self._durations, pace=self.pace, speed=self.speed)

    def _duration(self, beat: StageBeat) -> float:
        return beat.duration
