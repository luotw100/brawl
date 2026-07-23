"""阿瓦隆演出投影：真实整局存档 → 指令流的不变量与覆盖。

fixtures 是 random player 经 production run_game 跑出的真存档——投影与生成共用同一份
rules 产出，drift 无处藏身。跨多个 seed/配置收集 kind 并集，逼出 evil_chat/lady/刺杀路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from brawl.games.avalon import run as avalon_run
from brawl.games.avalon.protocol import (
    AvalonScene,
    Deal,
    GameOpen,
    GameOver,
    QuestReveal,
    Speech,
    VoteReveal,
)
from brawl.games.avalon.settings import AvalonSettings
from brawl.games.avalon.stagecast import apply, stagecast
from brawl.runtime.journal import load_replay_sentences
from brawl.stage.protocol import Directive, HostSpeech


class FakeMedia:
    """投影测试用的媒体桩：全量命中，URL 形状与 ReplayMediaResolver 一致。"""

    def audio(self, call_id: str) -> tuple[str, float]:
        return f"/replay/tts/{call_id}.opus", 1.0

    def avatar(self, seat: int) -> str:
        return f"/replay/avatars/{seat}.png"


_PLAN_KINDS = set(yaml.safe_load((Path(__file__).parents[1] / "plan.yaml").read_text(encoding="utf-8"))["directives"])


async def _run(replay: Path, **over: Any) -> list[Directive]:
    settings = AvalonSettings(players=over.pop("players", 6), seed=over.pop("seed", 42), player="random", replay=replay, render=False, **over)
    await avalon_run.run_game(settings)
    sentences = load_replay_sentences(replay)
    meta = json.loads((replay / "meta.json").read_text(encoding="utf-8"))
    return stagecast(sentences, meta, FakeMedia())


def _assert_invariants(directives: list[Directive]) -> None:
    kinds = [d.kind for d in directives]
    # 每个 kind 都被本包 plan.yaml 覆盖（host 是框架指令，走 host.placements）
    assert set(kinds) <= _PLAN_KINDS | {"host"}, f"plan.yaml 缺 kind：{set(kinds) - _PLAN_KINDS - {'host'}}"
    # 恰好一条终局指令；其后只有赛后闲聊与 host 收尾
    overs = [i for i, d in enumerate(directives) if isinstance(d, GameOver)]
    assert len(overs) == 1
    assert all(
        isinstance(d, HostSpeech) or (isinstance(d, Speech) and d.phase == "banter")
        for d in directives[overs[0] + 1:]
    )
    # 开局三件套打头
    assert isinstance(directives[0], GameOpen)
    assert isinstance(directives[1], HostSpeech) and directives[1].beat == "opening"
    assert isinstance(directives[2], Deal)
    # 配音拍 call_id 唯一；spoken 发言都带 call_id
    call_ids = [d.call_id for d in directives if d.call_id]
    assert len(call_ids) == len(set(call_ids))
    assert all(d.call_id for d in directives if isinstance(d, (Speech, HostSpeech)))
    # 聚合拍不空
    assert all(d.votes for d in directives if isinstance(d, VoteReveal))
    assert all(d.cards for d in directives if isinstance(d, QuestReveal))
    # reducer 全程折得动，终局 scene 自洽
    scene = AvalonScene()
    for seq, d in enumerate(directives, start=1):
        apply(scene, _feed_item(seq, d))
    over = directives[overs[0]]
    assert scene.winner == over.winner and scene.win_reason == over.win_reason
    assert scene.dealt and len(scene.seats) == directives[0].players


def _feed_item(seq: int, directive: Directive):
    from brawl.stage.protocol import FeedItem

    return FeedItem(seq=seq, directive=directive, started_at=0.0, duration=1.0)


@pytest.mark.asyncio
async def test_six_player_game_invariants(tmp_path) -> None:
    _assert_invariants(await _run(tmp_path / "g", players=6, seed=7))


@pytest.mark.asyncio
async def test_lady_flow_projected(tmp_path) -> None:
    """7 人带仙女：任务 3/4/5 后必有选验/揭牌/仙女发言/令牌交接。"""
    directives = await _run(tmp_path / "g", players=7, seed=11, lady=True)
    _assert_invariants(directives)
    kinds = [d.kind for d in directives]
    assert "lady_choose" in kinds and "lady_check" in kinds
    beats = [d.beat for d in directives if isinstance(d, HostSpeech)]
    assert "lady_choice" in beats and "lady" in beats


@pytest.mark.asyncio
async def test_evil_chat_and_assassination_covered_across_boards(tmp_path) -> None:
    """蓝方三任务胜 → 红方密谋 + 刺杀。随机 sampler 按 (seat,quest,attempt,phase) 定死，
    换 seed 不改票型、换人数才改——这组人数/seed 组合实测覆盖刺杀路径。"""
    kinds_seen: set[str] = set()
    phases_seen: set[str] = set()
    beats_seen: set[str] = set()
    for i, (players, seed) in enumerate(((5, 1), (8, 7), (9, 7))):
        directives = await _run(tmp_path / f"g{i}", players=players, seed=seed)
        _assert_invariants(directives)
        kinds_seen |= {d.kind for d in directives}
        phases_seen |= {d.phase for d in directives if isinstance(d, Speech)}
        beats_seen |= {d.beat for d in directives if isinstance(d, HostSpeech)}
    assert "assassination" in kinds_seen, f"选定板子没跑出刺杀局：{kinds_seen}"
    assert "evil_chat" in phases_seen
    assert "evil_chat_opening" in beats_seen
