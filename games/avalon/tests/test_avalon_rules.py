"""AvalonRules × core.sr.loop 端到端测试。"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from avalon.registry import load_registry
from avalon.rules_engine import AvalonRules, AvalonState
from avalon.settings import AvalonSettings
from avalon.state import Role
from avalon.validation import resolve_and_validate
from avalon.view import PlayerView
from core.sr.actor import RandomActor
from core.sr.journal import DirJournal
from core.sr.loop import resume, run
from core.threads import SeatThread


def _setup(seed: int = 42, players: int = 5) -> tuple[AvalonSettings, Any, Any]:
    registry = load_registry()
    settings = AvalonSettings(players=players, seed=seed, player="random")
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    return settings, registry, setup


async def _run(settings: AvalonSettings, registry: Any, setup: Any, rep: Path) -> AvalonState:
    rules = AvalonRules(registry, settings)
    actors = {no: RandomActor(rules.random_action) for no in range(1, len(setup.roles) + 1)}
    journal = DirJournal(rep, resume=False)
    threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        return await run(rules, setup, actors, journal, threads)
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_random_game_finishes(tmp_path) -> None:
    settings, registry, setup = _setup()
    final = await _run(settings, registry, setup, tmp_path / "rep")

    assert final.phase == "done"
    assert final.winner in ("good", "evil")
    assert final.win_reason in ("quests", "rejections", "assassination")

    journal = DirJournal(tmp_path / "rep", resume=True)
    sentences = journal.load_sentences()
    journal.close()
    assert len(sentences) > 0


@pytest.mark.asyncio
async def test_resume_matches_runtime(tmp_path) -> None:
    settings, registry, setup = _setup()
    rep = tmp_path / "rep"
    final = await _run(settings, registry, setup, rep)

    journal2 = DirJournal(rep, resume=True)
    rules2 = AvalonRules(registry, settings)
    threads2 = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        restored = await resume(rules2, actors={}, journal=journal2, threads=threads2, setup=setup)
    finally:
        journal2.close()

    assert restored.phase == final.phase
    assert restored.winner == final.winner
    assert restored.win_reason == final.win_reason


@pytest.mark.asyncio
async def test_cold_resume_without_snapshot_matches_runtime(tmp_path) -> None:
    """冷续跑（无快照全量重放）必须与实时终态逐字段一致。

    同时行动阶段（组队表决/任务出牌）一个 turn_id 多条句子：重放必须整组 apply 完
    再 advance，逐句 advance 会用不完整票型/牌堆结算（回归：真实崩局没有 _snapshot.json）。
    """
    settings, registry, setup = _setup()
    rep = tmp_path / "rep"
    final = await _run(settings, registry, setup, rep)
    (rep / "_snapshot.json").unlink()  # 崩局现场：没有终态快照，强制冷重放

    journal2 = DirJournal(rep, resume=True)
    rules2 = AvalonRules(registry, settings)
    threads2 = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        restored = await resume(rules2, actors={}, journal=journal2, threads=threads2, setup=setup)
    finally:
        journal2.close()

    assert restored.model_dump_json() == final.model_dump_json()


@pytest.mark.asyncio
async def test_midgame_crash_resume_reproduces_identical_log(tmp_path) -> None:
    """中段崩溃后续跑：确定性重放整局，日志逐行一致、串场词零重复。

    崩局形状：某个 turn 及之后全部丢失（并发行动的 turn 要么整组落盘要么整组丢失，
    不存在半组）。续跑重问该 turn，RandomActor 按 (seat,quest,attempt,phase) 定 seed，
    产出逐字节一致；host 串场按 turn_id 去重，已播报过的开场/开票不重记。
    """
    settings, registry, setup = _setup()
    rep = tmp_path / "rep"
    final = await _run(settings, registry, setup, rep)
    original = (rep / "sentences.jsonl").read_text(encoding="utf-8").splitlines()

    turns = [json.loads(line)["turn_id"] for line in original]
    cut_turn = turns[len(turns) // 2]
    cut = turns.index(cut_turn)  # cut_turn 首次出现的行号=崩溃点（该 turn 从未开始）
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    (crashed / "sentences.jsonl").write_text("\n".join(original[:cut]) + "\n", encoding="utf-8")

    rules2 = AvalonRules(registry, settings)
    journal = DirJournal(crashed, resume=True)
    threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        restored = await resume(rules2, actors={}, journal=journal, threads=threads, setup=setup)
        actors = {no: RandomActor(rules2.random_action) for no in range(1, len(setup.roles) + 1)}
        final2 = await run(rules2, setup, actors, journal, threads, state=restored)
    finally:
        journal.close()

    resumed = (crashed / "sentences.jsonl").read_text(encoding="utf-8").splitlines()
    assert resumed == original
    assert final2.model_dump_json() == final.model_dump_json()


@pytest.mark.asyncio
async def test_resume_into_terminal_appends_nothing(tmp_path) -> None:
    """终局后再 resume：closing 串场已播报过，run 零新增（host 去重守住终态重发）。"""
    settings, registry, setup = _setup()
    rep = tmp_path / "rep"
    await _run(settings, registry, setup, rep)
    n_before = len((rep / "sentences.jsonl").read_text(encoding="utf-8").splitlines())

    rules2 = AvalonRules(registry, settings)
    journal = DirJournal(rep, resume=True)
    threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        restored = await resume(rules2, actors={}, journal=journal, threads=threads, setup=setup)
        actors = {no: RandomActor(rules2.random_action) for no in range(1, len(setup.roles) + 1)}
        await run(rules2, setup, actors, journal, threads, state=restored)
    finally:
        journal.close()

    n_after = len((rep / "sentences.jsonl").read_text(encoding="utf-8").splitlines())
    assert n_after == n_before


@pytest.mark.asyncio
async def test_random_actor_produces_valid_json() -> None:
    settings, registry, setup = _setup()
    rules = AvalonRules(registry, settings)
    view = PlayerView(
        seat_no=1,
        role=Role.MERLIN,
        intel=[],
        players=5,
        team_size=2,
        phase="propose",
    )
    actor = RandomActor(rules.random_action)
    raw, audit = await actor.speak(1, view, "q1.a1.propose")
    assert "team" in raw
    assert audit["seat_no"] == 1


@pytest.mark.asyncio
async def test_random_game_with_lady_finishes(tmp_path) -> None:
    settings, registry, setup = _setup(players=7)
    settings = AvalonSettings(players=7, seed=42, player="random", lady=True)
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    final = await _run(settings, registry, setup, tmp_path / "rep")
    assert final.phase == "done"
