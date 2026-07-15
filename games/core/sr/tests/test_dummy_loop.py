"""DummyRules × loop/journal/resume 端到端回归。

验证：
1. loop 跑完 3 句后终局，threads 各席位的 inbox 收到了 3 条广播。
2. journal 落了 4 条 sentence（3 玩家 + 1 host 收场），meta 可写。
3. resume 从 snapshot 恢复的 state 与实时跑的终态相等。
4. resume 无 snapshot 冷续跑（传 setup）从零重放全部 sentences，仍得同一终态。
core 全程不读 state 字段，未触发任何玩法名词。
"""

from __future__ import annotations

import pytest

from core.sr.dummy import SEATS, DummyActor, DummyRules
from core.sr.journal import DirJournal, NullJournal
from core.sr.loop import resume, run
from core.threads import SeatThread


def _threads() -> dict[int, SeatThread]:
    return {no: SeatThread() for no in SEATS}


@pytest.mark.asyncio
async def test_run_routes_broadcast_and_records(tmp_path):
    rules = DummyRules()
    journal = DirJournal(tmp_path / "rep", resume=False)
    journal.write_meta({"game": "dummy"})
    actors = {no: DummyActor() for no in SEATS}
    threads = _threads()

    final = await run(rules, setup=None, actors=actors, journal=journal, threads=threads)

    # 终态：3 句均已 said
    assert final["step"] == 3
    assert len(final["said"]) == 3

    # clear-on-speak：席位发言后清自己的 inbox（等价旧 commit），故早发言者累积更多。
    # seat1（首发）inbox = 自己 + 后两人 + 收场 = 4；seat2 = 自己 + seat3 + 收场 = 3；
    # seat3（末发）= 自己 + 收场 = 2。收场广播在所有人之后、无人再清，全员末位都是它。
    assert len(threads[1].inbox) == 4
    assert len(threads[2].inbox) == 3
    assert len(threads[3].inbox) == 2
    for no in SEATS:
        assert threads[no].inbox[-1] == "主持人：本局结束。"

    # journal 落盘：4 条 sentence（3 玩家 + 1 host 收场）
    sentences = journal.load_sentences()
    assert len(sentences) == 4
    assert sentences[3]["speaker"] == "host"
    journal.close()


@pytest.mark.asyncio
async def test_resume_from_snapshot_matches_runtime(tmp_path):
    rep = tmp_path / "rep"
    rules = DummyRules()
    journal = DirJournal(rep, resume=False)
    journal.write_meta({"game": "dummy"})
    actors = {no: DummyActor() for no in SEATS}
    await run(rules, setup=None, actors=actors, journal=journal, threads=_threads())
    journal.close()

    # 重新打开（resume 模式），从 snapshot 恢复
    journal2 = DirJournal(rep, resume=True)
    restored = await resume(rules, actors={}, journal=journal2, threads=_threads(), setup=None)
    journal2.close()

    assert restored["step"] == 3
    assert len(restored["said"]) == 3


@pytest.mark.asyncio
async def test_resume_cold_no_snapshot_replays_all(tmp_path):
    """删掉 snapshot，从零重放全部 sentences 仍得同一终态——回放确定性。"""
    rep = tmp_path / "rep"
    rules = DummyRules()
    journal = DirJournal(rep, resume=False)
    journal.write_meta({"game": "dummy"})
    await run(rules, setup=None, actors={no: DummyActor() for no in SEATS}, journal=journal, threads=_threads())
    journal.close()

    # 删 snapshot，逼走冷续跑路径
    (rep / "_snapshot.json").unlink()
    journal2 = DirJournal(rep, resume=True)
    restored = await resume(rules, actors={}, journal=journal2, threads=_threads(), setup=None)
    journal2.close()

    assert restored["step"] == 3
    assert len(restored["said"]) == 3


@pytest.mark.asyncio
async def test_null_journal_no_disk(tmp_path):
    """无 --replay：NullRecorder 空对象，循环仍跑通、threads 仍路由，零落盘。"""
    rules = DummyRules()
    journal = NullJournal()
    actors = {no: DummyActor() for no in SEATS}
    threads = _threads()
    final = await run(rules, setup=None, actors=actors, journal=journal, threads=threads)

    assert final["step"] == 3
    # clear-on-speak 同 test_run_routes_broadcast_and_records：早发言者累积更多。
    assert len(threads[1].inbox) == 4
    assert len(threads[2].inbox) == 3
    assert len(threads[3].inbox) == 2
    for no in SEATS:
        assert threads[no].inbox[-1] == "主持人：本局结束。"
