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
async def test_resume_restores_inboxes(tmp_path):
    """resume 重放 deliveries：各席位 inbox 与崩溃前逐字一致（LLM 续跑不失忆）。

    截到 seat1 发言之后：seat1 的 inbox = 自己的那句（清过又广播回来），
    seat2/3 还没发言，inbox 累积 seat1 的广播；audience-only 内容绝不进 inbox。
    """
    rep = tmp_path / "rep"
    rules = DummyRules()
    journal = DirJournal(rep, resume=False)
    actors = {no: DummyActor() for no in SEATS}
    await run(rules, setup=None, actors=actors, journal=journal, threads=_threads())
    journal.close()

    lines = (rep / "sentences.jsonl").read_text(encoding="utf-8").splitlines()
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    (crashed / "sentences.jsonl").write_text(lines[0] + "\n", encoding="utf-8")  # 只留 seat1 那句

    journal2 = DirJournal(crashed, resume=True)
    threads = _threads()
    await resume(DummyRules(), actors={}, journal=journal2, threads=threads, setup=None)
    journal2.close()

    assert threads[1].inbox == ["1号说：大家好"]  # 发言人：清过后只余自己这句
    assert threads[2].inbox == ["1号说：大家好"]  # 未发言者：累积广播
    assert threads[3].inbox == ["1号说：大家好"]


@pytest.mark.asyncio
async def test_resume_drops_partial_tail_group(tmp_path):
    """旧式逐席落盘的崩局：尾部半组整组丢弃（state 与 inbox 都回滚），续跑重问该 turn。"""
    from core.sr.types import Delivery, Sentence

    class TwoSeatRules(DummyRules):
        """模拟同时行动：每步两个座位一起说。"""

        def expected(self, state: dict):
            from core.sr.types import Expectation

            step = state["step"]
            return Expectation(seats=[1, 2], turn_id=f"dual.{step}")

    rep = tmp_path / "rep"
    rep.mkdir()

    def sent(no: int, turn: str) -> Sentence:
        return Sentence(
            seq=-1, turn_id=turn, speaker=no, raw="x",
            deliveries=[Delivery(text=f"{no}号说：大家好", channels=["broadcast"])],
            action=f"{no}号说：大家好",
        )

    journal = DirJournal(rep, resume=True)
    # 完整组 dual.0（1、2 号）+ 半组 dual.1（只有 1 号）——旧式逐席落盘的断电现场
    journal.append_batch([sent(1, "dual.0"), sent(2, "dual.0")])
    journal.append_batch([sent(1, "dual.1")])

    threads = {1: SeatThread(), 2: SeatThread()}
    state = await resume(TwoSeatRules(), actors={}, journal=journal, threads=threads, setup=None)
    journal.close()

    assert state["step"] == 1  # 只有完整组 dual.0 结算（半组 dual.1 被丢弃）
    assert state["said"] == ["1号说：大家好", "2号说：大家好"]
    # 半组的 deliveries 也回滚：inbox 只剩 dual.0 的广播（发言人各自被清过）
    assert threads[1].inbox == ["1号说：大家好", "2号说：大家好"]
    assert threads[2].inbox == ["2号说：大家好"]


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
