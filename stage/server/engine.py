"""演出引擎：服务端唯一的"播放头"，也是舞台状态唯一的 source of truth。

一个 asyncio task 循环：取下一局 → 逐事件演出（adapt → apply 折进 Scene → publish 全量
状态 → 按时长停留）→ 幕间页 → 再取下一局。暂停/跳局/调速都是改这个 task 看的状态位。

publish 是注入的回调（app 层把"当前完整状态"广播给所有页面）；任何状态变化（每一拍、
暂停、旋钮、队列）都调一次 publish——客户端是纯渲染函数，收到什么画什么。

节奏模型与 games/avalon/replay.py 同源：pace 0~1 线性缩放每字停顿与事件间气口。
有离线 TTS 时（audio/durations.json），一拍时长取 max(文字估算, 音频时长+余量)，
保证音频永远放得完；文字打字机始终先于音频收尾。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

from server import scene as avalon
from server.protocol import (
    Assassination,
    Deal,
    Directive,
    FeedItem,
    GameOpen,
    GameOver,
    HostSpeech,
    Intermission,
    QuestReveal,
    Scene,
    Speech,
    VoteReveal,
)
from server.settings import StageSettings

# pace=1 时的上限（与 replay.py 的口径一致）
_MAX_CHAR_DELAY = 0.06
_MAX_BEAT = 0.7
_TICK = 0.2  # 状态位轮询粒度：暂停/跳局的最大响应延迟
_AUDIO_MARGIN = 0.5  # 音频时长之外多留的气口（秒）：页面预热到位后只兜残余抖动，不再扛拉取延迟


def _upcoming_audio(beats: list[Directive], i: int, factor: int) -> list[str]:
    """从第 i 拍之后顺取最多 factor 条待播音频 URL（跳过无声拍），交给页面提前预热——
    起拍那一刻音频已在缓存里，currentTime 从 0 即时出声，尾巴不再被下一拍顶掉时截断。"""
    urls: list[str] = []
    for directive in beats[i + 1:]:
        if len(urls) >= factor:  # 放循环头：factor=0（关掉预取）时直接不取
            break
        url = getattr(directive, "audio_url", None)
        if url:
            urls.append(url)
    return urls


class Engine:
    def __init__(self, settings: StageSettings, publish: Callable[[], Awaitable[None]]) -> None:
        self._root = settings.replay_root
        self._publish = publish
        self.scene = Scene()
        self.queue: list[str] = []
        self.paused = False
        self.pace = settings.pace
        self.intermission_seconds = settings.intermission_seconds
        self.loop_pool = settings.loop_pool
        self.feed_window = settings.feed_window
        self._prefetch_factor = settings.prefetch_factor
        self.phase: str = "idle"
        self.replay_id: str | None = None  # 只进带 token 的 /api/state，不进公开频道
        self.prefetch: list[str] = []  # 当前拍之后待播的音频 URL，进 StateMsg 让页面提前预热
        self._seq = 0
        self._skip = False
        self._pool_pick: str | None = None  # 取局缓存：保证"下一局已就绪"的判断与实际取局一致
        self._durations: dict[str, float] = {}  # 当前局的音频时长表（audio_url → 秒）

    # ---------------------------------------------------------------- 对外状态

    def library(self) -> list[str]:
        """replay_root 下所有可播的局（边界过滤：有 events.jsonl 才算）。"""
        return sorted(d.name for d in self._root.iterdir() if (d / "events.jsonl").is_file())

    def public_scene(self) -> Scene:
        """下发给页面的 Scene：feed 按窗口旋钮裁剪（服务端硬上限只是内部护栏）。"""
        return self.scene.model_copy(update={"feed": self.scene.feed[-self.feed_window:]})

    # ---------------------------------------------------------------- 导播台控制

    async def set_paused(self, paused: bool) -> None:
        self.paused = paused
        await self._publish()

    async def skip(self) -> None:
        self._skip = True

    async def set_knobs(self, *, pace: float | None = None, intermission_seconds: float | None = None,
                        loop_pool: bool | None = None, feed_window: int | None = None) -> None:
        if pace is not None:
            self.pace = pace
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

    async def _play(self, replay_id: str) -> GameOver | None:
        self.phase = "playing"
        self.replay_id = replay_id
        replay_dir = self._root / replay_id
        tts = avalon.load_tts(replay_dir)
        self._durations = avalon.audio_durations(replay_id, tts)
        names = avalon.load_names(replay_dir)  # 显示名随各局 meta 自带：不同局可用不同人设/网名
        host_name = avalon.load_host_name(replay_dir)
        # 先把整局摊平：前瞻要看后面的拍，懒迭代给不了"下一拍是谁"。一局事件量很小，全量无妨。
        beats = [avalon.adapt(event, replay_id=replay_id, tts=tts, names=names, host_name=host_name)
                 for event in avalon.load_events(replay_dir)]
        last: GameOver | None = None
        for i, directive in enumerate(beats):
            if isinstance(directive, GameOver):
                last = directive
            duration = self._duration(directive)
            self.prefetch = _upcoming_audio(beats, i, self._prefetch_factor)
            await self._emit(directive, duration)
            if not await self._dwell(duration):
                break  # 导播台跳局
        self.prefetch = []
        return last

    async def _intermission(self, last: GameOver | None) -> None:
        self.phase = "intermission"
        self.replay_id = None
        directive = Intermission(
            last_winner=last.winner if last else None,
            last_reason=last.win_reason if last else None,
            next_ready=bool(self.queue) or self._peek_pool() is not None,
        )
        await self._emit(directive, self.intermission_seconds)
        await self._dwell(self.intermission_seconds)

    async def _emit(self, directive: Directive, duration: float) -> None:
        self._seq += 1
        avalon.apply(self.scene, FeedItem(seq=self._seq, directive=directive, started_at=time.time(), duration=duration))
        await self._publish()

    async def _dwell(self, seconds: float) -> bool:
        """这一拍在台上停留：暂停时冻结倒计时，跳局立即返回 False。

        pace=0 时时长为 0，也必须让出一次事件循环——否则引擎独占线程，
        WS 广播、控制 API 全部饿死（实测会把内存吃爆）。"""
        await asyncio.sleep(0)
        remaining = seconds
        while remaining > 0:
            if self._skip:
                self._skip = False
                return False
            if self.paused:
                await asyncio.sleep(_TICK)
                continue
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

    def _duration(self, directive: Directive) -> float:
        """一拍演多久（秒）。分镜节奏配方写死在这（不做导播台）：以"从容直播"为基准编排——
        发牌每张约 1.5s、发言之间留约 3s 气口、表决逐个亮票、大场面敢留白。`unit = pace` 是
        秒预算单位：**pace=1 跑足这套从容节奏，pace=0 全归零（整局瞬间倾倒，生成/测试用）。**
        前端的发牌/亮票动画 stagger 由本拍 duration 反推，故任何 pace 下动画都跟得上、不被顶掉。

        两段式：先算 content（视觉/朗读，未计音频）→ 音频必须放得完取 max → 给有台词的拍
        加尾部气口（发言间留白）。音频时长按指令 audio_url 查表，与页面播的是同一条。"""
        char = self.pace * _MAX_CHAR_DELAY
        unit = self.pace  # 秒预算单位：pace=1 → 编排的秒数原样生效
        match directive:
            case Speech(text=text, monologue=monologue):
                content = (len(text) + len(monologue)) * char
            case HostSpeech(text=text):
                content = len(text) * char
            case Assassination(text=text, monologue=monologue):
                content = (len(text) + len(monologue)) * char
            case GameOpen():
                content = unit * 4.0
            case Deal():
                content = unit * (len(directive.seats) * 1.5 + 1.0)  # 逐张发牌 ~1.5s/张
            case VoteReveal():
                content = unit * (len(directive.votes) * 0.8 + 2.5)  # 逐个亮票 + 判定 hold
            case QuestReveal():
                content = unit * 6.0  # 慢起→摊牌→翻轨→闪屏
            case GameOver():
                content = unit * 12.0  # 逐个翻底牌的大结局
            case Intermission():
                return 0.0  # 幕间时长由 _intermission 用活旋钮控制，不在这算
            case _:
                raise ValueError(f"未覆盖的指令类型：{type(directive).__name__}")
        audio = self._durations.get(getattr(directive, "audio_url", None) or "")
        if audio is not None:
            content = max(content, audio + _AUDIO_MARGIN)
        # 尾部气口：有台词的拍之间留白，节奏从容；刺杀是终局炸点，留白要更狠
        tail = {Speech: 3.0, HostSpeech: 2.5, Assassination: 6.0}.get(type(directive), 0.0)
        return content + unit * tail
