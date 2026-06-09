"""引擎不变量：播完整局、暂停冻结、跳局、队列优先于循环池、idle 等排片、音频时长接管节奏。"""

from __future__ import annotations

import asyncio

from server.engine import Engine, _upcoming_audio
from server.protocol import GameOpen, Speech, VoteReveal
from server.tests.conftest import write_replay


def test_upcoming_audio_collects_next_urls_skipping_silent():
    """前瞻预取：跳过无声拍、顺取最多 factor 条；末拍后为空；factor=0 关掉预取。"""
    def speech(no: int, url: str | None) -> Speech:
        return Speech(phase="discuss", seat_no=no, text="x", monologue="", audio_url=url)

    beats: list = [
        GameOpen(players=5, good=3, evil=2),  # 无声
        speech(1, "/u1"),
        VoteReveal(quest=1, attempt=1, votes=[], approved=True, rejections=0),  # 无声，跳过
        speech(2, "/u2"),
        speech(3, "/u3"),
    ]
    assert _upcoming_audio(beats, 0, 2) == ["/u1", "/u2"]  # 越过 VoteReveal 取够 2 条
    assert _upcoming_audio(beats, 1, 2) == ["/u2", "/u3"]
    assert _upcoming_audio(beats, 4, 2) == []  # 末拍后面没有了
    assert _upcoming_audio(beats, 0, 0) == []  # factor=0 不预取


class Recorder:
    """替身 publish：把每次广播时的状态记下来供断言。"""

    def __init__(self) -> None:
        self.states = []
        self.engine: Engine | None = None

    async def publish(self) -> None:
        e = self.engine
        self.states.append((e.phase, e.public_scene().model_copy(deep=True)))

    def kinds(self):
        seen = set()
        out = []
        for _, scene in self.states:
            for item in scene.feed:
                if item.seq not in seen:
                    seen.add(item.seq)
                    out.append(item.directive.kind)
        return out


def make_engine(settings) -> tuple[Engine, Recorder]:
    rec = Recorder()
    engine = Engine(settings, rec.publish)
    rec.engine = engine
    return engine, rec


async def _run_until(engine: Engine, predicate, timeout: float = 5.0) -> None:
    task = asyncio.create_task(engine.run())
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()


async def test_plays_full_game_then_intermission(settings):
    engine, rec = make_engine(settings)
    await engine.set_queue(["game1"])
    await _run_until(engine, lambda: "intermission" in rec.kinds())
    kinds = rec.kinds()
    assert kinds[0] == "game_open" and "game_over" in kinds
    assert engine.scene.winner == "evil"


async def test_idle_when_queue_empty_and_pool_off(settings):
    engine, rec = make_engine(settings)
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.1)
    task.cancel()
    assert engine.phase == "idle"
    assert rec.kinds() == []


async def test_loop_pool_feeds_when_queue_empty(settings):
    settings = settings.model_copy(update={"loop_pool": True})
    engine, rec = make_engine(settings)
    await _run_until(engine, lambda: len(rec.kinds()) > 0)
    assert engine.phase != "idle"


async def test_pause_freezes_advance(settings):
    settings = settings.model_copy(update={"pace": 1.0})  # 给暂停留出反应窗口
    engine, rec = make_engine(settings)
    await engine.set_paused(True)
    await engine.set_queue(["game1"])
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0.8)
    task.cancel()
    # 暂停下最多演出第一拍后冻结，绝不会播完
    assert len(rec.kinds()) <= 1


async def test_skip_jumps_to_intermission(settings):
    settings = settings.model_copy(update={"pace": 1.0})
    engine, rec = make_engine(settings)
    await engine.set_queue(["game1"])
    task = asyncio.create_task(engine.run())
    try:
        async with asyncio.timeout(5):
            while not rec.kinds():
                await asyncio.sleep(0.01)
            await engine.skip()
            while "intermission" not in rec.kinds():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert "game_over" not in rec.kinds()  # 半途跳走，整局没播完


async def test_queue_precedes_pool(settings):
    write_replay(settings.replay_root, "game2")
    settings = settings.model_copy(update={"loop_pool": True})
    engine, rec = make_engine(settings)
    await engine.set_queue(["game2"])
    await _run_until(engine, lambda: engine.replay_id is not None or "game_open" in rec.kinds())
    assert engine.replay_id == "game2" or any(p == "playing" for p, _ in rec.states)


def test_library_only_lists_real_replays(settings):
    (settings.replay_root / "junk").mkdir()  # 没有 events.jsonl 的目录不算片
    engine, _ = make_engine(settings)
    assert engine.library() == ["game1"]


def test_public_scene_respects_feed_window(settings):
    engine, _ = make_engine(settings)
    engine.feed_window = 2
    from server.protocol import FeedItem, GameOpen

    for i in range(5):
        engine.scene.feed.append(FeedItem(seq=i, directive=GameOpen(players=5, good=3, evil=2),
                                          started_at=0.0, duration=1.0))
    assert len(engine.public_scene().feed) == 2
    assert len(engine.scene.feed) == 5  # 内部护栏不受窗口影响


async def test_audio_duration_overrides_text_estimate(settings):
    # manifest 的 duration_ms 接管节奏：音频时长 + 余量必须撑满这一拍。时长按指令的
    # audio_url 查表（页面播哪条、引擎按哪条停留同源），不再从 call_id 反推。
    engine, _ = make_engine(settings)
    engine._durations = {"/replays/game1/tts/a.opus": 9.0}
    voiced = Speech(phase="propose", seat_no=2, text="我带3号。", monologue="稳一手。",
                    audio_url="/replays/game1/tts/a.opus")
    silent = Speech(phase="discuss", seat_no=3, text="行吧。", monologue="没看出问题。")
    assert engine._duration(voiced) == 9.5  # pace=0 文字估算为 0，音频撑满
    assert engine._duration(silent) == 0.0  # 无音频回退字数估算


async def test_lady_audio_extends_dwell(settings):
    """仙女一拍压两次 LLM 调用（choose 私下决策 + announce 口播），事件用 announce_call_id
    标音频、无顶层 call_id。引擎按 adapt 拍板的 audio_url 取时长，整条 announce→audio_url→
    时长链路必须撑满这一拍——否则只按文字估算停留、口播被下一拍截断（曾经的真 bug）。"""
    from server import scene as avalon
    from server.tests import conftest

    lady_event = next(e for e in conftest.EVENTS if e["type"] == "lady")
    tts = {lady_event["announce_call_id"]: {"file": "lady.opus", "duration_ms": 20000}}
    directive = avalon.adapt(lady_event, replay_id="game1", tts=tts, names={}, host_name="司仪")
    engine, _ = make_engine(settings)  # pace=0 → 文字估算为 0
    engine._durations = avalon.audio_durations("game1", tts)
    assert engine._duration(directive) == 20.5  # 20s 音频 + 0.5 余量，而非文字估算的 0


async def test_broken_replay_skipped_channel_survives(settings):
    """坏 replay 绝不冻结频道：记日志、进幕间、继续播下一局。"""
    write_replay(settings.replay_root, "broken", events=[
        {"type": "game_start", "players": 5, "good": 3, "evil": 2},
        {"type": "nova_unknown"},
    ])
    engine, rec = make_engine(settings)
    await engine.set_queue(["broken", "game1"])
    await _run_until(engine, lambda: "game_over" in rec.kinds(), timeout=8)
    assert "intermission" in rec.kinds()  # 坏局之后照常进了幕间并播完了 game1


async def test_real_schema_full_replay(settings):
    """夹具 schema 与真实生成器对齐的守门员：整局喂进引擎必须零异常播到底。"""
    engine, rec = make_engine(settings)
    await engine.set_queue(["game1"])
    await _run_until(engine, lambda: "intermission" in rec.kinds())
    assert rec.kinds().count("game_over") == 1
