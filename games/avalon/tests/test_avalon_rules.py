"""AvalonRules × core.sr.loop 端到端测试。"""

from __future__ import annotations

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
