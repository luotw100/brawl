"""狼人杀插件的不变量：全事件类型覆盖、TTS 柔性、状态折叠、registry 派发。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from server import replay
from server.engine import Engine, FEED_CAP
from server.games.base import AdaptContext
from server.games.werewolf.plugin import WerewolfPlugin
from server.games.werewolf.protocol import WerewolfScene
from server.protocol import FeedItem, StateMsg
from server.registry import plugin_for_replay
from server.settings import StageSettings

PLUGIN = WerewolfPlugin()

WW_EVENTS = [
    {"type": "game_start", "board": "pre-witch-hunter-idiot-12", "board_name": "预女猎白12人场",
     "players": 4, "wolves": 1, "gods": 1, "villagers": 2, "seats": [
        {"no": 1, "persona": "fox", "strategy": "s", "role": "werewolf", "side": "werewolf", "model": "m"},
        {"no": 2, "persona": "shy", "strategy": "s", "role": "seer", "side": "good", "model": "m"},
        {"no": 3, "persona": "diva", "strategy": "s", "role": "villager", "side": "good", "model": "m"},
        {"no": 4, "persona": "boss", "strategy": "s", "role": "idiot", "side": "good", "model": "m"},
     ]},
    {"type": "deal", "seats": [
        {"no": 1, "persona": "fox", "strategy": "s", "role": "werewolf", "side": "werewolf", "model": "m"},
        {"no": 2, "persona": "shy", "strategy": "s", "role": "seer", "side": "good", "model": "m"},
        {"no": 3, "persona": "diva", "strategy": "s", "role": "villager", "side": "good", "model": "m"},
        {"no": 4, "persona": "boss", "strategy": "s", "role": "idiot", "side": "good", "model": "m"},
    ]},
    {"type": "host", "beat": "opening", "call_id": "1.1.host.opening", "text": "天黑请闭眼。"},
    {"type": "night_start", "day": 1},
    {"type": "wolf_kill", "day": 1, "target_no": 3, "votes": [
        {"seat_no": 1, "target_no": 3, "speech": "刀3。", "monologue": "先砍民。", "call_id": "1.1.wolf_kill.1"},
    ]},
    {"type": "seer_check", "day": 1, "call_id": "1.1.seer_check.2", "seat_no": 2,
     "target_no": 1, "result": "werewolf", "monologue": "查杀到手。"},
    {"type": "witch_action", "day": 1, "call_id": "1.1.witch_action.9", "seat_no": 2,
     "save": False, "poison_target_no": None, "monologue": "没有药。"},
    {"type": "death_resolved", "day": 1, "deaths": [
        {"seat_no": 3, "role": "villager", "reason": "wolf_kill", "day": 1},
    ]},
    {"type": "role_revealed", "day": 1, "seat_no": 3, "role": "villager", "reason": "wolf_kill"},
    {"type": "speech", "day": 1, "phase": "last_words", "call_id": "1.1.last_words.3",
     "seat_no": 3, "text": "我走了。", "monologue": "真倒霉。"},
    {"type": "vote_result", "day": 1, "votes": [
        {"seat_no": 1, "target_no": 4, "monologue": "推白痴。", "call_id": "1.1.vote.1"},
        {"seat_no": 2, "target_no": 1, "monologue": "出狼。", "call_id": "1.1.vote.2"},
    ], "exiled": 4, "tied": False},
    {"type": "idiot_revealed", "day": 1, "seat_no": 4},
    {"type": "vote_result", "day": 2, "votes": [
        {"seat_no": 1, "target_no": 2, "monologue": "继续冲。", "call_id": "2.1.vote.1"},
        {"seat_no": 2, "target_no": 1, "monologue": "归狼。", "call_id": "2.1.vote.2"},
    ], "exiled": 1, "tied": False},
    {"type": "role_revealed", "day": 2, "seat_no": 1, "role": "werewolf", "reason": "exile"},
    {"type": "hunter_shot", "day": 2, "call_id": "2.1.hunter_shoot.7", "hunter_no": 2,
     "target_no": None, "speech": "我不开枪。", "monologue": "稳住。"},
    {"type": "game_end", "winner": "good", "win_reason": "all_wolves_dead", "day": 2,
     "seats": [
        {"no": 1, "persona": "fox", "role": "werewolf", "side": "werewolf", "alive": False, "model": "m"},
        {"no": 2, "persona": "shy", "role": "seer", "side": "good", "alive": True, "model": "m"},
        {"no": 3, "persona": "diva", "role": "villager", "side": "good", "alive": False, "model": "m"},
        {"no": 4, "persona": "boss", "role": "idiot", "side": "good", "alive": True, "model": "m"},
     ],
     "deaths": [
        {"seat_no": 3, "role": "villager", "reason": "wolf_kill", "day": 1},
        {"seat_no": 1, "role": "werewolf", "reason": "exile", "day": 2},
     ]},
    {"type": "host", "beat": "reveal", "call_id": "2.1.host.reveal", "text": "公布身份。"},
    {"type": "banter", "call_id": "2.1.banter.2", "seat_no": 2, "role": "seer", "side": "good",
     "text": "查杀带队。", "monologue": "舒服。"},
    {"type": "host", "beat": "closing", "call_id": "2.1.host.closing", "text": "下一局见。"},
]

META = {
    "game": "werewolf",
    "setup": {
        "cast": ["fox", "shy", "diva", "boss"],
        "names": ["老狐狸", "社恐", "戏精", "大姐头"],
        "host_name": "司仪",
    },
    "model_labels": {"m": "Mock"},
}


def _write_ww_replay(root, name="ww1"):
    d = root / name
    d.mkdir()
    d.joinpath("events.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in WW_EVENTS), encoding="utf-8")
    d.joinpath("meta.json").write_text(json.dumps(META, ensure_ascii=False), encoding="utf-8")
    return d


def _ctx(replay_dir, replay_id="ww1"):
    return AdaptContext(
        replay_id=replay_id,
        tts=replay.load_tts(replay_dir),
        names=replay.load_names(replay_dir),
        host_name=replay.load_host_name(replay_dir),
        model_labels=replay.load_model_labels(replay_dir),
    )


def _adapt_all(replay_root):
    d = replay_root / "ww1"
    ctx = _ctx(d)
    return [PLUGIN.adapt(e, ctx) for e in WW_EVENTS]


def _fold_one(scene: WerewolfScene, directive, seq: int = 1) -> None:
    item = FeedItem(seq=seq, directive=directive, started_at=time.time(), duration=1.0)
    scene.feed.append(item)
    del scene.feed[:-FEED_CAP]
    PLUGIN.apply(scene, item)


def _fold(directives) -> WerewolfScene:
    scene = PLUGIN.new_scene()
    for seq, directive in enumerate((d for d in directives if d is not None), start=1):
        _fold_one(scene, directive, seq)
    return scene


class _Recorder:
    def __init__(self) -> None:
        self.states = []
        self.engine: Engine | None = None

    async def publish(self) -> None:
        engine = self.engine
        self.states.append((engine.phase, engine.public_scene().model_copy(deep=True)))

    def kinds(self) -> list[str]:
        seen: set[int] = set()
        kinds: list[str] = []
        for _, scene in self.states:
            for item in scene.feed:
                if item.seq not in seen:
                    seen.add(item.seq)
                    kinds.append(item.directive.kind)
        return kinds


async def _run_until(engine: Engine, predicate, timeout: float = 5.0) -> None:
    task = asyncio.create_task(engine.run())
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()


def _make_engine(replay_root) -> tuple[Engine, _Recorder]:
    settings = StageSettings(
        replay_root=replay_root,
        stage_token="st4ge",
        console_token="t0k3n",
        pace=0.0,
        intermission_seconds=0.0,
        loop_pool=False,
    )
    rec = _Recorder()
    engine = Engine(settings, rec.publish)
    rec.engine = engine
    return engine, rec


@pytest.fixture
def ww_root(tmp_path):
    root = tmp_path / "replays"
    root.mkdir()
    _write_ww_replay(root)
    return root


def test_registry_picks_werewolf_plugin(ww_root):
    assert plugin_for_replay(ww_root / "ww1").id == "werewolf"


def test_adapt_covers_every_event_type(ww_root):
    directives = _adapt_all(ww_root)
    assert [d.kind if d is not None else None for d in directives] == [
        "ww_game_open", "ww_deal", "host", "ww_night_start", "ww_wolf_kill",
        "ww_seer_check", "ww_witch_action", "ww_death_resolved", "ww_role_reveal",
        "ww_speech", "ww_vote_reveal", "ww_idiot_reveal", "ww_vote_reveal",
        "ww_role_reveal", "ww_hunter_shot", "ww_game_over", None, "ww_speech", "host",
    ]
    assert directives[2].name == "司仪"


def test_adapt_unknown_event_raises():
    with pytest.raises(ValueError, match="未知事件类型"):
        PLUGIN.adapt({"type": "nova"}, AdaptContext(replay_id="g", tts={}, names={}))


def test_missing_tts_keeps_directives_silent(ww_root):
    directives = _adapt_all(ww_root)
    speech = next(d for d in directives if getattr(d, "kind", "") == "ww_speech")
    assert speech.audio_url is None


def test_audio_url_from_manifest(ww_root):
    d = ww_root / "ww1"
    (d / "tts").mkdir()
    (d / "tts" / "manifest.json").write_text(json.dumps({
        "1.1.last_words.3": {"file": "1.1.last_words.3.opus", "duration_ms": 1200},
        "1.1.host.opening": {"file": "1.1.host.opening.opus", "duration_ms": 800},
    }), encoding="utf-8")
    directives = _adapt_all(ww_root)
    speech = next(d for d in directives if getattr(d, "kind", "") == "ww_speech")
    assert speech.audio_url == "/replays/ww1/tts/1.1.last_words.3.opus"
    assert directives[2].audio_url == "/replays/ww1/tts/1.1.host.opening.opus"


def test_state_folds_full_game(ww_root):
    scene = _fold(_adapt_all(ww_root))
    assert scene.game == "werewolf"
    assert scene.board_name == "预女猎白12人场"
    assert scene.dealt is True
    assert scene.winner == "good" and scene.win_reason == "all_wolves_dead"
    assert scene.phase == "postgame"
    assert [(s.no, s.name, s.alive, s.can_vote) for s in scene.seats] == [
        (1, "老狐狸", False, True),
        (2, "社恐", True, True),
        (3, "戏精", False, True),
        (4, "大姐头", True, True),
    ]
    assert [d.seat_no for d in scene.deaths] == [3, 1]
    assert [t.kind for t in scene.timeline] == ["ww_death", "ww_vote", "ww_vote", "ww_death"]


def test_timeline_serializes_concrete_fields_for_stage(ww_root):
    scene = _fold(_adapt_all(ww_root))
    dumped = StateMsg(phase="playing", scene=scene, overlays=[]).model_dump(mode="json")
    death, vote = dumped["scene"]["timeline"][:2]
    assert death == {
        "kind": "ww_death",
        "day": 1,
        "seat_no": 3,
        "role": "villager",
        "reason": "wolf_kill",
    }
    assert vote == {
        "kind": "ww_vote",
        "day": 1,
        "exiled": 4,
        "tied": False,
        "votes": [
            {"seat_no": 1, "target_no": 4, "monologue": "推白痴。"},
            {"seat_no": 2, "target_no": 1, "monologue": "出狼。"},
        ],
    }


def test_game_open_resets_previous_game(ww_root):
    directives = _adapt_all(ww_root)
    scene = _fold(directives)
    _fold_one(scene, directives[0], seq=99)
    assert scene.winner is None and scene.deaths == [] and scene.timeline == []


async def test_engine_plays_silent_werewolf_replay_to_intermission(ww_root):
    engine, rec = _make_engine(ww_root)
    await engine.set_queue(["ww1"])
    await _run_until(engine, lambda: "intermission" in rec.kinds())

    kinds = rec.kinds()
    assert kinds[0] == "ww_game_open"
    assert "ww_game_over" in kinds
    assert engine.scene.game == "werewolf"
    assert engine.scene.winner == "good"
    assert all(
        getattr(item.directive, "audio_url", None) is None
        for _, scene in rec.states
        for item in scene.feed
        if item.directive.kind != "intermission"
    )
