"""AvalonStubRules × core.sr.loop 端到端冒烟测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from avalon.sr_rules import PLAYERS, AvalonStubRules
from core.sr.dummy import DummyActor
from core.sr.journal import DirJournal
from core.sr.loop import resume, run
from core.threads import SeatThread


def _threads() -> dict[int, SeatThread]:
    return {no: SeatThread() for no in range(1, PLAYERS + 1)}


async def _run_game(tmp_path, *, resume: bool = False) -> tuple[dict, Path]:
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=resume)
    journal.write_meta({"game": "avalon-stub"})
    rules = AvalonStubRules()
    actors = {no: DummyActor() for no in range(1, PLAYERS + 1)}
    final = await run(rules, setup=None, actors=actors, journal=journal, threads=_threads())
    journal.close()
    return final, rep


@pytest.mark.asyncio
async def test_avalon_stub_run(tmp_path) -> None:
    final, rep = await _run_game(tmp_path)

    assert final["phase"] == "done"
    assert len(final["results"]) == 1
    assert final["results"][0] == "fail"  # 队伍含红方，触发失败

    # sentences：night reveal + 5 speeches + 1 propose + 1 host announce +
    #            5 votes + 1 vote result + 2 quest + 1 quest result = 17
    journal = DirJournal(rep, resume=True)
    sentences = journal.load_sentences()
    journal.close()
    assert len(sentences) == 17


@pytest.mark.asyncio
async def test_avalon_stub_resume(tmp_path) -> None:
    final, rep = await _run_game(tmp_path)
    assert final["phase"] == "done"

    journal2 = DirJournal(rep, resume=True)
    restored = await resume(
        AvalonStubRules(),
        actors={},
        journal=journal2,
        threads=_threads(),
        setup=None,
    )
    journal2.close()
    assert restored["phase"] == "done"
    assert restored["results"] == final["results"]
