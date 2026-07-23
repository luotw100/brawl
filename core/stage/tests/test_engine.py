"""单局引擎不变量：从头演到尾、首帧门语义、feed 窗口、音频时长接管节奏。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avalon.protocol import Deal, GameOpen, GameOver, SeatInfo, Speech, VoteReveal
from avalon.stagecast import STAGE_GAME as AVALON
from core.stage import stagecast as registry_mod
from core.stage.engine import Engine, _upcoming_audio
from core.stage.playplan import StageBeat
from core.stage.settings import EngineKnobs
from core.stage.stagecast import StageGame


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


# ---------------------------------------------------------------- 假玩法整局


_DIRECTIVES = [
    GameOpen(players=5, good=3, evil=2),
    Deal(seats=[SeatInfo(no=1, name="甲", persona="fox", role="merlin", side="good", model="m")], leader_no=1, lady_holder=None),
    Speech(phase="discuss", seat_no=1, text="过。", monologue="", audio_url="/replay/tts/3.1.opus"),
    GameOver(winner="good", win_reason="quests", seats=[]),
]


def _fake_game() -> StageGame:
    return StageGame(
        id="fake",
        stagecast=lambda sentences, meta, media: list(_DIRECTIVES),
        new_scene=AVALON.new_scene,
        apply=AVALON.apply,
        summary=AVALON.summary,
        is_opening_anchor=AVALON.is_opening_anchor,
        terminal=AVALON.terminal,
        plan_path=AVALON.plan_path,
    )


def _replay(tmp_path: Path) -> Path:
    d = tmp_path / "game"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"game": "fake"}), encoding="utf-8")
    (d / "sentences.jsonl").write_text("", encoding="utf-8")
    return d


class Recorder:
    def __init__(self) -> None:
        self.states = []

    async def publish(self, engine: Engine) -> None:
        self.states.append(engine.public_scene().model_copy(deep=True))

    def kinds(self) -> list[str]:
        seen: set[int] = set()
        out: list[str] = []
        for scene in self.states:
            for item in scene.feed:
                if item.seq not in seen:
                    seen.add(item.seq)
                    out.append(item.directive.kind)
        return out


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setattr(registry_mod, "_REGISTRY", {"fake": _fake_game()})


async def test_plays_full_game_and_folds_scene(tmp_path, fake_registry) -> None:
    rec = Recorder()
    engine = Engine(EngineKnobs(speed=1000.0), publish=lambda: rec.publish(engine))
    await engine.play(_replay(tmp_path))

    assert rec.kinds() == ["game_open", "deal", "speech", "game_over"]
    final = engine.public_scene()
    assert final.winner == "good" and final.dealt
    assert engine.prefetch == []
    assert engine.summary(_DIRECTIVES[-1])[0] == "终局"


async def test_opening_anchor_gate_holds_clock_until_first_frame(tmp_path, fake_registry) -> None:
    class Gate:
        @property
        def hold_opening_frame(self) -> bool:
            return True

        async def first_frame_published(self, seq: int) -> float:
            return 123.0

    published_clock: list[tuple[str, bool, float]] = []

    async def publish() -> None:
        feed = engine.scene.feed
        if feed:
            published_clock.append((feed[-1].directive.kind, feed[-1].clock_running, feed[-1].started_at))

    engine = Engine(EngineKnobs(speed=1000.0), publish=publish, playback_gate=Gate())
    await engine.play(_replay(tmp_path))

    deal_frames = [f for f in published_clock if f[0] == "deal"]
    # 首拍（发牌=首帧门）先按静止帧下发，门闩回报后以门闩时刻重发并启动时钟
    assert deal_frames[0][1] is False
    assert deal_frames[-1][1:] == (True, 123.0)


async def test_unknown_game_fails_loudly(tmp_path, fake_registry) -> None:
    d = tmp_path / "ghost"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"game": "ghost"}), encoding="utf-8")
    (d / "sentences.jsonl").write_text("", encoding="utf-8")

    engine = Engine(EngineKnobs(), publish=lambda: None)
    with pytest.raises(ValueError, match="未登记的玩法"):
        await engine.play(d)
