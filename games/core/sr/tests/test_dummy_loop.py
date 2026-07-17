"""DummyRules × loop/journal/resume 端到端回归。

验证：
1. 一个 expectation 与其 host 句子共用 commit_id。
2. live 与 resume 以同一次序恢复 inbox 和私有 messages。
3. 快照不能跳过逻辑 commit 校验，也不改变 thread 恢复结果。
4. renderer 只看到已分配 seq/commit_id 的已提交句子。
core 全程不读 state 字段，未触发任何玩法名词。
"""

from __future__ import annotations

import pytest

from core.sr.dummy import SEATS, DummyActor, DummyRules
from core.sr.journal import DirJournal, NullJournal
from core.sr.loop import resume, run
from core.sr.types import Delivery, Expectation, ParseResult, Sentence
from core.threads import SeatThread


def _threads() -> dict[int, SeatThread]:
    return {no: SeatThread() for no in SEATS}


class TwoSeatSimultaneousRules(DummyRules):
    """一批两个席位同时广播，用于钉住批次发布语义。"""

    def is_terminal(self, state: dict) -> bool:
        return state["step"] >= 1

    def expected(self, state: dict) -> Expectation:
        return Expectation(seats=[1, 2], turn_id="simultaneous.0")

    def pending(self, state: dict):
        return []


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
    assert [s["commit_id"] for s in sentences] == [0, 1, 2, 2]
    assert [len(threads[no].messages) for no in SEATS] == [2, 2, 2]
    assert threads[1].messages[0].content == "你是 1 号，请说一句话。"
    assert threads[1].messages[1].content == "1号说：大家好"
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
    assert [m.model_dump() for m in threads[1].messages] == [
        {"role": "human", "content": "你是 1 号，请说一句话。"},
        {"role": "assistant", "content": "1号说：大家好"},
    ]


@pytest.mark.asyncio
async def test_resume_rejects_logical_half_turn_even_if_caller_committed_it(tmp_path):
    """Journal 保证物理原子；若 loop 以外的调用者误提交半组，resume fail-fast。"""
    class TwoSeatRules(DummyRules):
        """模拟同时行动：每步两个座位一起说。"""

        def expected(self, state: dict):
            from core.sr.types import Expectation

            step = state["step"]
            return Expectation(seats=[1, 2], turn_id=f"dual.{step}")

    rep = tmp_path / "rep"
    rep.mkdir()

    def sent(no: int, turn: str) -> Sentence:
        text = f"{no}号说：大家好"
        return Sentence(
            seq=-1, turn_id=turn, speaker=no, raw=text,
            prompt=f"你是 {no} 号，请说一句话。",
            deliveries=[Delivery(text=text, channels=["broadcast"])],
            action=text,
        )

    journal = DirJournal(rep, resume=True)
    # 完整 dual.0 + 误用 API 提交的半个 dual.1。新运行时自身不会这样写。
    journal.append_batch([sent(1, "dual.0"), sent(2, "dual.0")])
    journal.append_batch([sent(1, "dual.1")])
    # 即便有人又为这份逻辑半组保存了哈希绑定的合法快照，快照也不能掩盖 commit 错误。
    journal.save_snapshot(
        journal.last_seq(),
        TwoSeatRules().pack({"step": 0, "said": []}),
    )

    threads = {1: SeatThread(), 2: SeatThread()}
    with pytest.raises(RuntimeError, match="席位不完整"):
        await resume(TwoSeatRules(), actors={}, journal=journal, threads=threads, setup=None)
    journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shape", "message"),
    [
        ("before_player", "player sentence cannot follow a host sentence"),
        ("missing", "host 句子缺失"),
        ("forged", "host 句子缺失"),
        ("bad_prompt", "prompt 与规则视图不一致"),
    ],
)
async def test_resume_rejects_host_rows_that_live_loop_cannot_produce(
    tmp_path,
    shape: str,
    message: str,
) -> None:
    """host 必须紧随玩家且与 apply/advance 两次 pending 的确定输出逐字段一致。"""
    rules = DummyRules()
    journal = DirJournal(tmp_path / "rep", resume=False)

    def player(no: int) -> Sentence:
        text = f"{no}号说：大家好"
        return Sentence(
            seq=-1,
            turn_id=f"dummy.{no - 1}.say",
            speaker=no,
            raw=text,
            prompt=f"你是 {no} 号，请说一句话。",
            deliveries=[Delivery(text=text, channels=["broadcast"])],
            action=text,
        )

    def closing(text: str = "主持人：本局结束。") -> Sentence:
        return Sentence(
            seq=-1,
            turn_id="dummy.host.closing",
            speaker="host",
            deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
            action='{"kind":"closing"}',
        )

    journal.append_batch([player(1)])
    journal.append_batch([player(2)])
    if shape == "before_player":
        journal.append_batch([closing(), player(3)])
    elif shape == "missing":
        journal.append_batch([player(3)])
    elif shape == "forged":
        journal.append_batch([player(3), closing("伪造收场")])
    else:
        bad = player(3)
        bad.prompt = "伪造 prompt"
        journal.append_batch([bad, closing()])

    with pytest.raises(RuntimeError, match=message):
        await resume(rules, actors={}, journal=journal, threads=_threads(), setup=None)
    journal.close()


@pytest.mark.asyncio
async def test_resume_with_snapshot_matches_runtime(tmp_path):
    rep = tmp_path / "rep"
    rules = DummyRules()
    journal = DirJournal(rep, resume=False)
    journal.write_meta({"game": "dummy"})
    actors = {no: DummyActor() for no in SEATS}
    live_threads = _threads()
    await run(rules, setup=None, actors=actors, journal=journal, threads=live_threads)
    journal.close()

    # 重新打开（snapshot 存在，但恢复真相仍来自完整 commit 日志）
    journal2 = DirJournal(rep, resume=True)
    restored_threads = _threads()
    restored = await resume(rules, actors={}, journal=journal2, threads=restored_threads, setup=None)
    journal2.close()

    assert restored["step"] == 3
    assert len(restored["said"]) == 3
    assert {
        no: thread.model_dump() for no, thread in restored_threads.items()
    } == {no: thread.model_dump() for no, thread in live_threads.items()}


@pytest.mark.asyncio
async def test_resume_cold_no_snapshot_replays_all(tmp_path):
    """删掉 snapshot 后仍得同一终态——恢复正确性不依赖派生 checkpoint。"""
    rep = tmp_path / "rep"
    rules = DummyRules()
    journal = DirJournal(rep, resume=False)
    journal.write_meta({"game": "dummy"})
    live_threads = _threads()
    await run(
        rules,
        setup=None,
        actors={no: DummyActor() for no in SEATS},
        journal=journal,
        threads=live_threads,
    )
    journal.close()

    # 删 snapshot，结果不变
    (rep / "_snapshot.json").unlink()
    journal2 = DirJournal(rep, resume=True)
    restored_threads = _threads()
    restored = await resume(rules, actors={}, journal=journal2, threads=restored_threads, setup=None)
    journal2.close()

    assert restored["step"] == 3
    assert len(restored["said"]) == 3
    assert {
        no: thread.model_dump() for no, thread in restored_threads.items()
    } == {no: thread.model_dump() for no, thread in live_threads.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize("journal_kind", ["dir", "null"])
async def test_renderer_only_sees_assigned_runtime_ids(tmp_path, journal_kind):
    class CapturingRenderer:
        def __init__(self) -> None:
            self.ids: list[tuple[int, int]] = []

        def set_meta(self, meta) -> None:
            pass

        def render_sentence(self, sentence) -> None:
            self.ids.append((sentence.seq, sentence.commit_id))

    journal = DirJournal(tmp_path / "rep", resume=False) if journal_kind == "dir" else NullJournal()
    renderer = CapturingRenderer()
    await run(
        DummyRules(),
        setup=None,
        actors={no: DummyActor() for no in SEATS},
        journal=journal,
        threads=_threads(),
        renderer=renderer,
    )
    journal.close()

    assert renderer.ids == [(0, 0), (1, 1), (2, 2), (3, 2)]


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


@pytest.mark.asyncio
async def test_simultaneous_speakers_all_receive_the_whole_batch_on_live_and_resume(tmp_path):
    """同批 actor 先一起清 inbox；物理顺序靠后的发言不能抹掉前一人的话。"""
    rules = TwoSeatSimultaneousRules()
    journal = DirJournal(tmp_path / "rep", resume=False)
    live_threads = {1: SeatThread(), 2: SeatThread()}
    await run(
        rules,
        setup=None,
        actors={1: DummyActor(), 2: DummyActor()},
        journal=journal,
        threads=live_threads,
    )

    expected = ["1号说：大家好", "2号说：大家好"]
    assert live_threads[1].inbox == expected
    assert live_threads[2].inbox == expected

    restored_threads = {1: SeatThread(), 2: SeatThread()}
    await resume(rules, actors={}, journal=journal, threads=restored_threads, setup=None)
    assert {
        seat: thread.model_dump() for seat, thread in restored_threads.items()
    } == {seat: thread.model_dump() for seat, thread in live_threads.items()}
    journal.close()


@pytest.mark.asyncio
async def test_unknown_route_is_rejected_before_journal_or_threads_change(tmp_path):
    class InvalidRouteRules(DummyRules):
        def parse(self, seat: int, raw: str, state: dict) -> ParseResult:
            return ParseResult(
                deliveries=[
                    Delivery(text=raw, channels=["broadcast"]),
                    Delivery(text="坏路由", channels=[99]),
                ],
                action=raw,
            )

    journal = DirJournal(tmp_path / "rep", resume=False)
    threads = _threads()
    with pytest.raises(RuntimeError, match="unknown seats"):
        await run(
            InvalidRouteRules(),
            setup=None,
            actors={seat: DummyActor() for seat in SEATS},
            journal=journal,
            threads=threads,
        )

    assert journal.last_seq() == -1
    assert journal.load_sentences() == []
    assert all(thread.inbox == [] and thread.messages == [] for thread in threads.values())
    journal.close()


@pytest.mark.asyncio
async def test_resume_rejects_unknown_route_before_clearing_threads(tmp_path):
    """外部篡改出的毒路由在恢复边界整体失败，不能留下半恢复 thread。"""
    journal = DirJournal(tmp_path / "rep", resume=False)
    raw = "1号说：大家好"
    journal.append_batch([
        Sentence(
            seq=-1,
            turn_id="dummy.0.say",
            speaker=1,
            raw=raw,
            prompt="你是 1 号，请说一句话。",
            deliveries=[Delivery(text=raw, channels=[99])],
            action=raw,
        )
    ])
    threads = {
        seat: SeatThread(inbox=[f"保留{seat}"])
        for seat in SEATS
    }

    with pytest.raises(RuntimeError, match="unknown seats"):
        await resume(DummyRules(), actors={}, journal=journal, threads=threads, setup=None)

    assert {seat: thread.inbox for seat, thread in threads.items()} == {
        seat: [f"保留{seat}"] for seat in SEATS
    }
    journal.close()
