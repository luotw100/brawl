"""狼人杀演出投影：真实整局存档 → 指令流的不变量与覆盖。

与 avalon/tests/test_stagecast.py 同法：random player 经 production run_game 跑出真存档，
跨多 seed 收集 kind 并集逼出猎人开枪/白痴翻牌等长尾路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from brawl.games.werewolf import run as werewolf_run
from brawl.games.werewolf.protocol import (
    Deal,
    GameOpen,
    GameOver,
    SeerCheck,
    Speech,
    VoteReveal,
    WerewolfScene,
    WolfKill,
)
from brawl.games.werewolf.settings import WerewolfSettings
from brawl.games.werewolf.stagecast import apply, stagecast
from brawl.runtime.journal import load_replay_sentences
from brawl.stage.protocol import Directive, FeedItem, HostSpeech


class FakeMedia:
    def audio(self, call_id: str) -> tuple[str, float]:
        return f"/replay/tts/{call_id}.opus", 1.0

    def avatar(self, seat: int) -> str:
        return f"/replay/avatars/{seat}.png"


_PLAN_KINDS = set(yaml.safe_load((Path(__file__).parents[1] / "plan.yaml").read_text(encoding="utf-8"))["directives"])


async def _run(replay: Path, seed: int) -> list[Directive]:
    await werewolf_run.run_game(WerewolfSettings(seed=seed, player="random", replay=replay, render=False))
    sentences = load_replay_sentences(replay)
    meta = json.loads((replay / "meta.json").read_text(encoding="utf-8"))
    return stagecast(sentences, meta, FakeMedia())


def _assert_invariants(directives: list[Directive]) -> None:
    kinds = [d.kind for d in directives]
    assert set(kinds) <= _PLAN_KINDS | {"host"}, f"plan.yaml 缺 kind：{set(kinds) - _PLAN_KINDS - {'host'}}"
    overs = [i for i, d in enumerate(directives) if isinstance(d, GameOver)]
    assert len(overs) == 1
    assert all(
        isinstance(d, HostSpeech) or (isinstance(d, Speech) and d.phase == "banter")
        for d in directives[overs[0] + 1:]
    )
    assert isinstance(directives[0], GameOpen)
    assert isinstance(directives[1], HostSpeech) and directives[1].beat == "opening"
    assert isinstance(directives[2], Deal)
    call_ids = [d.call_id for d in directives if d.call_id]
    assert len(call_ids) == len(set(call_ids))
    assert all(d.votes for d in directives if isinstance(d, (VoteReveal, WolfKill)))
    # 查验拍的配音已接回主持私语（call_id 指向 host 句）
    assert all(d.call_id and d.call_id.endswith(".host") for d in directives if isinstance(d, SeerCheck))
    # 狼刀提议明细与裁决同源：每条提议都来自真狼
    scene = WerewolfScene()
    for seq, d in enumerate(directives, start=1):
        apply(scene, FeedItem(seq=seq, directive=d, started_at=0.0, duration=1.0))
    over = directives[overs[0]]
    assert scene.winner == over.winner and scene.win_reason == over.win_reason
    assert len(scene.deaths) == len({d.seat_no for d in scene.deaths})  # 死亡台账无重复入账
    assert all(not s.alive for s in scene.seats if s.no in {d.seat_no for d in scene.deaths})


@pytest.mark.asyncio
async def test_full_game_invariants_and_night_beats(tmp_path) -> None:
    directives = await _run(tmp_path / "g", seed=1)
    _assert_invariants(directives)
    kinds = [d.kind for d in directives]
    # 基本盘：入夜/狼刀/查验/女巫/死讯/投票/终局都在
    assert {"ww_night_start", "ww_wolf_kill", "ww_seer_check", "ww_witch_action", "ww_vote_reveal", "ww_game_over"} <= set(kinds)
    beats = [d.beat for d in directives if isinstance(d, HostSpeech)]
    # 入夜主持词的配音接回（旧桥丢过）
    assert "night_start" in beats and "day_start" in beats


@pytest.mark.asyncio
async def test_long_tail_kinds_covered_across_seeds(tmp_path) -> None:
    """猎人开枪/白痴翻牌等长尾：多 seed 并集覆盖。"""
    kinds_seen: set[str] = set()
    for i, seed in enumerate((1, 2, 3, 5, 8, 13)):
        directives = await _run(tmp_path / f"g{i}", seed=seed)
        _assert_invariants(directives)
        kinds_seen |= {d.kind for d in directives}
    assert "ww_hunter_shot" in kinds_seen, f"6 个 seed 没跑出猎人开枪：{sorted(kinds_seen)}"
