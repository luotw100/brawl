"""演出引擎：服务端唯一的"播放头"，也是舞台状态唯一的 source of truth。

一个 asyncio task 循环：取下一局 → 逐事件演出（adapt → apply 折进 Scene → publish 全量
状态 → 按时长停留）→ 幕间页 → 再取下一局。下一拍/跳局/调速都是改这个 task 看的状态位。

玩法无关：引擎只通过 `GamePlugin`（registry 按 meta.game 派发）认识一局对局——adapt/apply/
节奏配方/音效/终局判定全是插件的事，引擎从不 import 或 match 任何具体指令类型。加玩法即并列
一个插件，引擎零分叉。

publish 是注入的回调（app 层把"当前完整状态"广播给所有页面）；任何状态变化（每一拍、
旋钮、队列）都调一次 publish——客户端是纯渲染函数，收到什么画什么。

节奏模型与 games/avalon/replay.py 同源：pace 0~1 线性缩放每字停顿与事件间气口。
有离线 TTS 时（tts/manifest.json），一拍时长取 max(文字估算, 音频时长+余量)，
保证音频永远放得完；文字打字机始终先于音频收尾。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from server import registry, replay
from server.games.base import AdaptContext, GamePlugin
from server.protocol import (
    BaseScene,
    Directive,
    FeedItem,
    GameResult,
    HostSpeech,
    Intermission,
    NarrationCue,
)
from server.settings import StageSettings

# pace=1 时的上限（与 replay.py 的口径一致）
_MAX_CHAR_DELAY = 0.06
_TICK = 0.2  # 状态位轮询粒度：跳局/下一拍的最大响应延迟
_AUDIO_MARGIN = 0.5  # 音频时长之外多留的气口（秒）：页面预热到位后只兜残余抖动，不再扛拉取延迟
_NARRATION_MARGIN = 0.2  # 主持旁白贴在动画下，只留很短缓冲，不再按独立节目拍留尾巴
FEED_CAP = 5  # 服务端只留最近几拍（窗口旋钮 1~3 再从中裁剪下发）


@dataclass
class StageBeat:
    directive: Directive
    content_at: float = 0.0
    narrations: list[NarrationCue] = field(default_factory=list)


class PlaybackGate(Protocol):
    async def first_frame_published(self, seq: int) -> float | None: ...
    async def before_intermission(self) -> None: ...


class NullPlaybackGate:
    async def first_frame_published(self, seq: int) -> float | None:
        return None

    async def before_intermission(self) -> None:
        pass


def _beat_audio_urls(beat: StageBeat) -> list[str]:
    urls = [n.audio_url for n in beat.narrations if n.audio_url]
    url = getattr(beat.directive, "audio_url", None)
    if url:
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

    def library(self) -> list[str]:
        """replay_root 下所有可播的局（边界过滤：对局跑完即可；TTS 柔性，详见 replay.is_broadcastable）。"""
        return sorted(d.name for d in self._root.iterdir() if d.is_dir() and replay.is_broadcastable(d))

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
            self._pool_pick = random.choice(library)
        return self._pool_pick

    async def _play(self, replay_id: str) -> GameResult | None:
        self.phase = "playing"
        self.replay_id = replay_id
        replay_dir = self._root / replay_id
        self._plugin = registry.plugin_for_replay(replay_dir)  # 按 meta.game 换玩法插件
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
            duration = self._duration(beat)
            self.prefetch = _upcoming_audio(beats, i, self._prefetch_factor)
            await self._emit(beat, duration)
            if self._plugin.is_opening_anchor(directive):
                started_at = await self._playback_gate.first_frame_published(self._seq)
                if started_at is not None:
                    self.scene.feed[-1].started_at = started_at
                    await self._publish()
            if not await self._dwell(duration):
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
        await self._emit(StageBeat(directive), self.intermission_seconds)
        await self._dwell(self.intermission_seconds)

    async def _emit(self, beat: StageBeat, duration: float) -> None:
        self._seq += 1
        item = FeedItem(
            seq=self._seq,
            directive=beat.directive,
            started_at=time.time(),
            duration=duration,
            speed=self.speed,
            content_at=beat.content_at,
            narrations=beat.narrations,
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

    def _stage_beats(self, directives: list[Directive]) -> list[StageBeat]:
        """把 replay 的 host 事件贴到相邻视觉拍上：games 决定说什么，stage 决定怎么播。

        哪些主持 beat 当 lead-in 先播完（如阿瓦隆的 propose_intro/lady）由插件 lead_in_beats() 决定；
        其余 host 事件压在刚出现的视觉拍上（opening/deal/vote/quest 等结果口播）。"""
        lead_in = self._plugin.lead_in_beats()
        beats: list[StageBeat] = []
        pending: list[HostSpeech] = []
        for directive in directives:
            if isinstance(directive, HostSpeech):
                if directive.beat in lead_in:
                    pending.append(directive)
                elif beats:
                    self._append_post_narration(beats[-1], directive)
                else:
                    pending.append(directive)
                continue

            beat = StageBeat(directive)
            at = 0.0
            for host in pending:
                cue = self._narration(host, at)
                beat.narrations.append(cue)
                at += cue.duration
            beat.content_at = at
            pending = []
            beats.append(beat)

        if pending and beats:
            for host in pending:
                self._append_post_narration(beats[-1], host)
        return beats

    def _append_post_narration(self, beat: StageBeat, host: HostSpeech) -> None:
        at = 0.0
        if self._plugin.speech_paced(beat.directive):
            at = beat.content_at + self._content_duration(beat.directive)
        if beat.narrations:
            at = max(at, max(n.at + n.duration for n in beat.narrations))
        beat.narrations.append(self._narration(host, at))

    def _narration(self, host: HostSpeech, at: float) -> NarrationCue:
        return NarrationCue(
            name=host.name,
            beat=host.beat,
            text=host.text,
            audio_url=host.audio_url,
            at=at,
            duration=self._narration_duration(host),
        )

    def _narration_duration(self, host: HostSpeech) -> float:
        char = self.pace * _MAX_CHAR_DELAY
        content = len(host.text) * char
        audio = self._durations.get(host.audio_url or "")
        if audio is not None:
            content = max(content, audio + _NARRATION_MARGIN)
        return content / self.speed

    def _content_duration(self, directive: Directive) -> float:
        """主内容时长（秒）：插件给出视觉/朗读秒数 → 音频必须放得完取 max → 除以倍速。
        音频时长按指令 audio_url 查表，与页面播的是同一条。"""
        content = self._plugin.content_seconds(directive, self.pace)
        audio = self._durations.get(getattr(directive, "audio_url", None) or "")
        if audio is not None:
            content = max(content, audio + _AUDIO_MARGIN)
        return content / self.speed

    def _tail_duration(self, directive: Directive) -> float:
        return self._plugin.tail_seconds(directive, self.pace) / self.speed

    def _duration(self, beat: StageBeat) -> float:
        """一拍演多久（秒）。分镜节奏配方在插件里（每玩法自定，不做导播台）：以"从容直播"为基准——
        发牌每张约 1.5s、发言之间留约 3s 气口、表决逐个亮票、大场面敢留白。**pace=1 跑足这套从容
        节奏，pace=0 全归零（整局瞬间倾倒，生成/测试用）。** 前端的发牌/亮票动画 stagger 由本拍
        duration 反推，故任何 pace 下动画都跟得上、不被顶掉。

        两段式：先算 content（视觉/朗读 + 音频地板，插件 + 引擎合成）→ 给有台词的拍加尾部气口。
        揭晓拍（发牌/表决/任务开牌）若挂了主持旁白，则整拍时长由旁白撑满（动画跟着旁白走）。"""
        narration_end = max((n.at + n.duration for n in beat.narrations), default=0.0)
        if beat.narrations and self._plugin.reveal_paced_by_narration(beat.directive):
            return narration_end
        content = beat.content_at + self._content_duration(beat.directive) + self._tail_duration(beat.directive)
        for narration in beat.narrations:
            content = max(content, narration.at + narration.duration)
        # speed 是整条时间轴的除数：每拍（含音频地板）等比压缩 → 引擎少 sleep（原生快录/快播）。
        # 音频另在播放侧按 speed 提速（直播 playbackRate / 录制 atempo）贴合压缩后的拍，音画仍齐。
        return content
