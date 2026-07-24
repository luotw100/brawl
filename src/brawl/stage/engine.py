"""演出引擎：单局播放头，舞台状态唯一的 source of truth。

本期只做录屏：`play(replay_dir)` 把一局存档从头演到尾——stagecast 投影成指令流、
plan.yaml 编译成确定性 beats、逐拍 emit（折进 Scene → publish 全量状态 → 按时长停留），
演完返回。频道调度（取局/幕间/循环池）在直播回归时是本类之上的另一层，不进这里。

玩法无关：引擎只通过 `StageGame`（registry 按 meta.game 派发）认识一局对局；
分镜节奏、主持贴靠、音效 cue 全部由玩法包 plan.yaml 描述，经通用 playplan compiler
编译。引擎从不 import 或 match 任何具体指令类型。

publish 是注入的回调（server 层把"当前完整状态"广播给所有页面）；任何状态变化
（每一拍、首帧门）都调一次 publish——客户端是纯渲染函数，收到什么画什么。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from brawl.runtime.journal import load_replay_sentences
from brawl.stage.media import ReplayMediaResolver
from brawl.stage.playplan import StageBeat, compile_stage_beats, load_plan
from brawl.stage.protocol import BaseScene, Directive, FeedItem
from brawl.stage.stagecast import StageGame, game_for

FEED_CAP = 5  # 服务端只留最近几拍（下发窗口再从中裁剪）
FEED_WINDOW = 2  # 对话区显示最近几拍（当前发言为主角）；画面美学常数，不是用户旋钮
PREFETCH_FACTOR = 2  # 提前几拍把待播音频 URL 露给页面预热（起拍即出声、尾巴不被截）
MAX_SPEED = 16  # 页面 playbackRate 上限（Chrome 超界抛 NotSupportedError 会打断整页渲染）


class PlaybackGate(Protocol):
    """录制门闩：首帧 paint 对齐与收屏时机的事实源（Null = 不等门，直接演）。"""

    @property
    def hold_opening_frame(self) -> bool: ...
    async def first_frame_published(self, seq: int) -> float | None: ...


class NullPlaybackGate:
    @property
    def hold_opening_frame(self) -> bool:
        return False

    async def first_frame_published(self, seq: int) -> float | None:
        return None


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
    """单局播放头。publish 回调由装配层注入；scene 在 play 期间持有。"""

    def __init__(
        self,
        speed: float,
        publish: Callable[[], Awaitable[None]],
        playback_gate: PlaybackGate | None = None,
    ) -> None:
        # speed：时间轴倍速（每拍时长含音频地板 ÷speed → 少 sleep、原生快录/快播；
        # 音频按 speed 提速贴合压缩后的拍）。范围护栏与 CLI 校验同口径。
        if not 0 < speed <= MAX_SPEED:
            raise ValueError(f"speed 必须在 (0, {MAX_SPEED}]，收到 {speed}（页面 playbackRate 上限 {MAX_SPEED}）")
        self._speed = speed
        self._publish = publish
        self._gate = playback_gate or NullPlaybackGate()
        self.scene: BaseScene | None = None
        self.prefetch: list[str] = []  # 当前拍之后待播的音频 URL，进 StateMsg 让页面提前预热
        self._seq = 0
        self._game: StageGame | None = None

    # ---------------------------------------------------------------- 对外状态

    def public_scene(self) -> BaseScene:
        """下发给页面的 Scene：feed 按窗口旋钮裁剪（服务端硬上限只是内部护栏）。"""
        assert self.scene is not None, "play 尚未开始，没有 scene"
        return self.scene.model_copy(update={"feed": self.scene.feed[-FEED_WINDOW:]})

    def summary(self, directive: Directive) -> tuple[str, str, str]:
        """一拍 → 录制进度行（委托当前玩法 bundle；record 的节拍日志用）。"""
        assert self._game is not None and self.scene is not None
        return self._game.summary(directive, self.scene)

    # ---------------------------------------------------------------- 主流程

    async def play(self, replay_dir: Path) -> None:
        """把一局存档从头演到尾。加载期失败（坏存档/契约漂移）在这里抛出——
        录制路径大声失败，不静默吞。"""
        meta = json.loads((replay_dir / "meta.json").read_text(encoding="utf-8"))
        game = game_for(meta["game"])
        self._game = game
        sentences = load_replay_sentences(replay_dir)
        media = ReplayMediaResolver(replay_dir)
        directives = game.stagecast(sentences, meta, media)
        plan = load_plan(game.plan_path)
        beats = compile_stage_beats(directives, plan, media.durations(), speed=self._speed)

        self.scene = game.new_scene()
        for i, beat in enumerate(beats):
            directive = beat.directive
            self.prefetch = _upcoming_audio(beats, i, PREFETCH_FACTOR)
            opening_anchor = game.is_opening_anchor(directive)
            hold_clock = opening_anchor and self._gate.hold_opening_frame
            await self._emit(beat, clock_running=not hold_clock)
            if opening_anchor:
                started_at = await self._gate.first_frame_published(self._seq)
                if started_at is not None:
                    self.scene.feed[-1].started_at = started_at
                if hold_clock:
                    self.scene.feed[-1].clock_running = True
                if started_at is not None or hold_clock:
                    await self._publish()
            await self._dwell(beat.duration)
        self.prefetch = []

    async def _emit(self, beat: StageBeat, *, clock_running: bool = True) -> None:
        assert self.scene is not None and self._game is not None
        self._seq += 1
        item = FeedItem(
            seq=self._seq,
            directive=beat.directive,
            started_at=time.time(),
            duration=beat.duration,
            clock_running=clock_running,
            speed=self._speed,
            content_at=beat.content_at,
            narrations=beat.narrations,
            tracks=beat.tracks,
        )
        # feed 入队 + 窗口护栏是玩法无关的，引擎统一做；玩法 reducer 只折自己的状态。
        self.scene.feed.append(item)
        del self.scene.feed[:-FEED_CAP]
        self._game.apply(self.scene, item)
        await self._publish()

    async def _dwell(self, seconds: float) -> None:
        """这一拍在台上停留。时长为 0 时也必须让出一次事件循环——否则引擎独占线程，
        WS 广播、门闩回调全部饿死（实测会把内存吃爆）。取消由外层传播。"""
        await asyncio.sleep(0)
        if seconds > 0:
            await asyncio.sleep(seconds)
