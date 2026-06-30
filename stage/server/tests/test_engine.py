"""引擎不变量：播完整局、暂停冻结、跳局、队列优先于循环池、idle 等排片、音频时长接管节奏。"""

from __future__ import annotations

import asyncio

from server.engine import Engine, StageBeat, _upcoming_audio
from server.games.avalon.protocol import Deal, GameOpen, QuestCard, QuestReveal, SeatVote, Speech, VoteReveal
from server.protocol import HostSpeech
from server.tests.conftest import EVENTS, write_replay


def test_upcoming_audio_collects_next_urls_skipping_silent():
    """前瞻预取：跳过无声拍、顺取最多 factor 条；末拍后为空；factor=0 关掉预取。"""
    def speech(no: int, url: str | None) -> Speech:
        return Speech(phase="discuss", seat_no=no, text="x", monologue="", audio_url=url)

    beats = [
        StageBeat(GameOpen(players=5, good=3, evil=2)),  # 无声
        StageBeat(speech(1, "/u1")),
        StageBeat(VoteReveal(quest=1, attempt=1, votes=[], approved=True, rejections=0)),  # 无声，跳过
        StageBeat(speech(2, "/u2")),
        StageBeat(speech(3, "/u3")),
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


def make_engine(settings, playback_gate=None) -> tuple[Engine, Recorder]:
    rec = Recorder()
    engine = Engine(settings, rec.publish, playback_gate=playback_gate)
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


async def test_playback_gate_starts_on_deal_frame(settings):
    class Gate:
        async def first_frame_published(self, seq: int) -> float | None:
            assert seq == 2
            return 1234.0

        async def before_intermission(self) -> None:
            pass

    engine, rec = make_engine(settings, Gate())
    await engine.set_queue(["game1"])
    await _run_until(engine, lambda: len([s for _, s in rec.states if s.feed and s.feed[-1].seq == 2]) >= 2)
    deal_items = [scene.feed[-1] for _, scene in rec.states if scene.feed and scene.feed[-1].seq == 2]
    assert deal_items[-1].started_at == 1234.0


async def test_playback_gate_runs_before_intermission_is_published(settings):
    class Gate:
        def __init__(self) -> None:
            self.stopped = False

        async def first_frame_published(self, seq: int) -> float | None:
            return None

        async def before_intermission(self) -> None:
            self.stopped = True

    gate = Gate()
    engine, rec = make_engine(settings, gate)
    await engine.set_queue(["game1"])
    await _run_until(engine, lambda: "intermission" in rec.kinds())
    intermission_index = next(i for i, (_, scene) in enumerate(rec.states)
                              if scene.feed and scene.feed[-1].directive.kind == "intermission")
    assert gate.stopped
    assert all(scene.feed[-1].directive.kind != "intermission" for _, scene in rec.states[:intermission_index])


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


async def test_step_advances_past_dwell(settings):
    """下一拍：本拍停留还没走完就提前结束、推进到下一拍（不像跳局那样 break 整局）。"""
    settings = settings.model_copy(update={"pace": 1.0})  # 长停留，确保不是自然走完
    engine, rec = make_engine(settings)
    await engine.set_queue(["game1"])
    task = asyncio.create_task(engine.run())
    try:
        async with asyncio.timeout(5):
            while not rec.kinds():  # 第一拍上台、正在它的长停留里
                await asyncio.sleep(0.01)
            await engine.step()
            while len(rec.kinds()) < 2:  # 下一拍被提前推上来
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
    assert "game_open" in rec.kinds()  # 整局没被跳走，照常从开局拍开始往下推


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


def test_library_only_lists_broadcastable_replays(settings):
    """进库闸只剩"对局跑完"这一道：裸目录/没跑完挡住；没配音的局照进（TTS 柔性，无声照常播）。"""
    (settings.replay_root / "junk").mkdir()  # 没有 events.jsonl 的目录不算片
    write_replay(settings.replay_root, "silent", voiced=False)  # 跑完但 TTS 没生成 → 仍可播
    unfinished = [e for e in EVENTS if e["type"] != "game_end"]  # 缺终局事件 = 没跑完
    write_replay(settings.replay_root, "running", events=unfinished)
    engine, _ = make_engine(settings)
    assert engine.library() == ["game1", "silent"]


def test_library_includes_replay_with_missing_audio_file(settings):
    """TTS 柔性：manifest 留着记录但 .opus 被删（换声/搬运掉了），这局照样进库——缺张就那拍静默。"""
    game = write_replay(settings.replay_root, "holey")
    next(game.joinpath("tts").glob("*.opus")).unlink()
    engine, _ = make_engine(settings)
    assert "holey" in engine.library()


def test_public_scene_respects_feed_window(settings):
    engine, _ = make_engine(settings)
    engine.feed_window = 2
    from server.games.avalon.protocol import GameOpen
    from server.protocol import FeedItem

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
    assert engine._duration(StageBeat(voiced)) == 9.5  # pace=0 文字估算为 0，音频撑满
    assert engine._duration(StageBeat(silent)) == 0.0  # 无音频回退字数估算


async def test_speed_compresses_beat_duration(settings):
    # speed 是整条时间轴的除数：每拍时长等比缩短 → 引擎少 sleep、原生快录（音频另在播放侧提速贴合）
    engine, _ = make_engine(settings)
    engine._durations = {"/replays/game1/tts/a.opus": 9.0}
    voiced = Speech(phase="propose", seat_no=2, text="我带3号。", monologue="稳一手。",
                    audio_url="/replays/game1/tts/a.opus")
    assert engine._duration(StageBeat(voiced)) == 9.5  # speed=1.0 基准（9s 音频 + 0.5 余量）
    engine.speed = 2.0
    assert engine._duration(StageBeat(voiced)) == 4.75  # 2× → 拍减半


async def test_lady_audio_extends_dwell(settings):
    """仙女一拍压两次 LLM 调用（choose 私下决策 + announce 口播），事件用 announce_call_id
    标音频、无顶层 call_id。引擎按 adapt 拍板的 audio_url 取时长，整条 announce→audio_url→
    时长链路必须撑满这一拍——否则只按文字估算停留、口播被下一拍截断（曾经的真 bug）。"""
    from server import replay
    from server.games.avalon.plugin import AvalonPlugin
    from server.games.base import AdaptContext
    from server.tests import conftest

    lady_event = next(e for e in conftest.EVENTS if e["type"] == "lady")
    tts = {lady_event["announce_call_id"]: {"file": "lady.opus", "duration_ms": 20000}}
    directive = AvalonPlugin().adapt(lady_event, AdaptContext(replay_id="game1", tts=tts, names={}, host_name="司仪"))
    engine, _ = make_engine(settings)  # pace=0 → 文字估算为 0
    engine._durations = replay.audio_durations("game1", tts)
    assert engine._duration(StageBeat(directive)) == 20.5  # 20s 音频 + 0.5 余量，而非文字估算的 0


async def test_host_speech_collapses_into_adjacent_stage_beat(settings):
    engine, _ = make_engine(settings)
    directives = [
        GameOpen(players=5, good=3, evil=2),
        Deal(seats=[], leader_no=5, lady_holder=4),
        HostSpeech(name="司仪", beat="opening", text="开场", audio_url="/h1"),
        Speech(phase="propose", seat_no=2, text="我带3号。", monologue="", audio_url="/p"),
        VoteReveal(quest=1, attempt=1, votes=[], approved=True, rejections=0),
        HostSpeech(name="司仪", beat="vote_result", text="通过", audio_url="/h2"),
    ]
    beats = engine._stage_beats(directives)
    assert [b.directive.kind for b in beats] == ["game_open", "deal", "speech", "vote_reveal"]
    assert [(n.beat, n.audio_url) for n in beats[1].narrations] == [("opening", "/h1")]
    assert [(n.beat, n.audio_url) for n in beats[3].narrations] == [("vote_result", "/h2")]


async def test_lead_host_delays_player_content(settings):
    engine, _ = make_engine(settings)
    engine._durations = {"/h": 2.0}
    beats = engine._stage_beats([
        HostSpeech(name="司仪", beat="propose_intro", text="队长提队", audio_url="/h"),
        Speech(phase="propose", seat_no=2, text="我带3号。", monologue="", audio_url=None),
    ])
    assert len(beats) == 1
    assert beats[0].content_at == 2.2
    assert beats[0].narrations[0].at == 0.0


async def test_reveal_animation_duration_follows_narration(settings):
    engine, _ = make_engine(settings.model_copy(update={"pace": 1.0}))
    engine._durations = {"/h": 3.0}
    host = HostSpeech(name="司仪", beat="vote_result", text="十票赞成", audio_url="/h")
    beat = StageBeat(
        VoteReveal(
            quest=1,
            attempt=1,
            votes=[SeatVote(seat_no=i, approve=True, monologue="") for i in range(1, 11)],
            approved=True,
            rejections=0,
        ),
        narrations=[engine._narration(host, 0.0)],
    )
    assert engine._duration(beat) == 3.2


async def test_deal_animation_duration_follows_opening_narration(settings):
    engine, _ = make_engine(settings.model_copy(update={"pace": 1.0}))
    engine._durations = {"/h": 5.0}
    host = HostSpeech(name="司仪", beat="opening", text="欢迎来到阿瓦隆", audio_url="/h")
    beat = StageBeat(
        Deal(seats=[], leader_no=5, lady_holder=4),
        narrations=[engine._narration(host, 0.0)],
    )
    assert engine._duration(beat) == 5.2


async def test_quest_animation_duration_follows_narration(settings):
    engine, _ = make_engine(settings.model_copy(update={"pace": 1.0}))
    engine._durations = {"/h": 4.0}
    host = HostSpeech(name="司仪", beat="quest_result", text="任务成功", audio_url="/h")
    beat = StageBeat(
        QuestReveal(
            quest=1,
            team=[1, 2, 3],
            cards=[QuestCard(seat_no=1, success=True, monologue="")],
            fails=0,
            fails_required=1,
            result="success",
        ),
        narrations=[engine._narration(host, 0.0)],
    )
    assert engine._duration(beat) == 4.2


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
