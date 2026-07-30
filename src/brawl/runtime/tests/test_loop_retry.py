"""loop 的 speak+parse 同单元重试语义。

只有 actor 明确标记的瞬态失败与 JsonParseError（坏 JSON、非法目标）会重试；
普通编程错误立即失败。重试上限后 fail-fast，绝不把非法产出写进日志。
重试间隔用 monkeypatch 打掉的 _sleep，测试零等待。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import brawl.runtime.loop as loop_mod
from brawl.runtime.errors import FatalActorError, RetryableActorError
from brawl.runtime.journal import NullJournal
from brawl.runtime.loop import run
from brawl.runtime.parsing import JsonParseError
from brawl.runtime.tests.dummy import SEATS, DummyActor, DummyRules
from brawl.runtime.threads import SeatThread
from brawl.runtime.types import Expectation


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(delay: float) -> None:
        return None

    monkeypatch.setattr(loop_mod, "_sleep", _instant)


class FlakySpeakActor:
    """前 fail_times 次 speak 抛异常，之后正常。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.rollbacks = 0
        self.commits = 0

    async def speak(self, seat: int, view: Any, turn_id: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RetryableActorError("模拟瞬态网络错误")
        return f"{seat}号说：大家好"

    def commit_turn(self, seat: int) -> None:
        self.commits += 1

    async def rollback(self, seat: int) -> None:
        self.rollbacks += 1


class FlakyParseActor:
    """前 fail_times 次返回非法产出（DummyRules.parse 只接受非空文本，空串抛 ValueError）。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.rollbacks = 0
        self.commits = 0

    async def speak(self, seat: int, view: Any, turn_id: str) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            return ""  # 空产出：语义校验不过
        return f"{seat}号说：大家好"

    def commit_turn(self, seat: int) -> None:
        self.commits += 1

    async def rollback(self, seat: int) -> None:
        self.rollbacks += 1


class StrictDummyRules(DummyRules):
    """空产出视为非法（模拟玩法 parse 硬校验）。"""

    def parse(self, seat: int, raw: str, state: dict):
        if not raw.strip():
            raise JsonParseError("空产出非法")
        return super().parse(seat, raw, state)


def _threads() -> dict[int, SeatThread]:
    return {no: SeatThread() for no in SEATS}


class OneTurnTwoSeatRules(DummyRules):
    def is_terminal(self, state: dict) -> bool:
        return state["step"] >= 1

    def expected(self, state: dict) -> Expectation:
        return Expectation(seats=[1, 2], turn_id="simultaneous.0")

    def pending(self, state: dict):
        return []


@pytest.mark.asyncio
async def test_flaky_speak_recovers_within_retries(tmp_path):
    actor = FlakySpeakActor(fail_times=2)
    actors = {no: (actor if no == SEATS[0] else FlakySpeakActor(0)) for no in SEATS}
    final = await run(DummyRules(), setup=None, actors=actors, journal=NullJournal(), threads=_threads())
    assert final["step"] == 3
    assert actor.calls == 3  # 2 次失败 + 第 3 次成功


@pytest.mark.asyncio
async def test_parse_failure_triggers_respeak(tmp_path):
    actor = FlakyParseActor(fail_times=2)
    actors = {no: (actor if no == SEATS[0] else FlakyParseActor(0)) for no in SEATS}
    final = await run(StrictDummyRules(), setup=None, actors=actors, journal=NullJournal(), threads=_threads())
    assert final["step"] == 3
    assert actor.calls == 3  # parse 失败也算一次失败调用，重拿一份新产出
    assert actor.rollbacks == 2  # 每次 parse 失败都回执 rollback（清 session 失败轮）
    assert actor.commits == 1  # 最终通过才 commit


@pytest.mark.asyncio
async def test_parse_failure_is_fed_back_to_retrying_actor() -> None:
    class FeedbackActor(FlakyParseActor):
        def __init__(self) -> None:
            super().__init__(fail_times=1)
            self.feedback: list[str] = []

        def set_retry_feedback(self, seat: int, feedback: str) -> None:
            self.feedback.append(feedback)

    actor = FeedbackActor()
    actors = {no: (actor if no == SEATS[0] else FlakyParseActor(0)) for no in SEATS}
    await run(
        StrictDummyRules(),
        setup=None,
        actors=actors,
        journal=NullJournal(),
        threads=_threads(),
    )

    assert actor.feedback == ["空产出非法"]


@pytest.mark.asyncio
async def test_parse_failure_feedback_includes_rejected_nonempty_draft() -> None:
    class DraftRules(DummyRules):
        def parse(self, seat: int, raw: str, state: dict):
            if raw == "坏草稿":
                raise JsonParseError("与公开承诺矛盾")
            return super().parse(seat, raw, state)

    class FeedbackActor(FlakyParseActor):
        def __init__(self) -> None:
            super().__init__(fail_times=0)
            self.feedback: list[str] = []

        async def speak(self, seat: int, view: Any, turn_id: str) -> str:
            self.calls += 1
            return "坏草稿" if self.calls == 1 else f"{seat}号说：已修正"

        def set_retry_feedback(self, seat: int, feedback: str) -> None:
            self.feedback.append(feedback)

    actor = FeedbackActor()
    actors = {no: (actor if no == SEATS[0] else FlakyParseActor(0)) for no in SEATS}
    await run(
        DraftRules(),
        setup=None,
        actors=actors,
        journal=NullJournal(),
        threads=_threads(),
    )

    assert actor.feedback == ["与公开承诺矛盾\n被拒绝的原草稿：坏草稿"]


@pytest.mark.asyncio
async def test_journal_batches_turn_atomically(tmp_path):
    """一个 turn 的玩家句子 + host 串场同批落盘（追加一次成型），seq 连续无洞。"""
    import json

    from brawl.runtime.journal import DirJournal

    journal = DirJournal(tmp_path / "rep", resume=False)
    actors = {no: DummyActor() for no in SEATS}
    await run(DummyRules(), setup=None, actors=actors, journal=journal, threads=_threads())
    journal.close()
    rows = [json.loads(ln) for ln in (tmp_path / "rep" / "sentences.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["seq"] for r in rows] == list(range(len(rows)))  # seq 从 0 连续
    # 3 个玩家 turn 各一句，host 收场句并入末批紧随 seat3
    speakers = [r["speaker"] for r in rows]
    assert speakers == [1, 2, 3, "host"]


@pytest.mark.asyncio
async def test_retry_exhaustion_fails_fast(tmp_path):
    actor = FlakySpeakActor(fail_times=loop_mod._MAX_RETRIES)  # 每次都失败
    actors = {no: (actor if no == SEATS[0] else FlakySpeakActor(0)) for no in SEATS}
    with pytest.raises(RetryableActorError):
        await run(DummyRules(), setup=None, actors=actors, journal=NullJournal(), threads=_threads())
    assert actor.calls == loop_mod._MAX_RETRIES  # 恰好试满上限，不多调


@pytest.mark.asyncio
async def test_parse_exhaustion_fails_fast(tmp_path):
    actor = FlakyParseActor(fail_times=loop_mod._MAX_RETRIES)
    actors = {no: (actor if no == SEATS[0] else FlakyParseActor(0)) for no in SEATS}
    with pytest.raises(JsonParseError, match="空产出非法"):
        await run(StrictDummyRules(), setup=None, actors=actors, journal=NullJournal(), threads=_threads())
    assert actor.calls == loop_mod._MAX_RETRIES


@pytest.mark.asyncio
async def test_unclassified_actor_error_is_not_retried():
    class BrokenActor(FlakySpeakActor):
        async def speak(self, seat: int, view: Any, turn_id: str) -> str:
            self.calls += 1
            raise RuntimeError("actor programming bug")

    actor = BrokenActor(0)
    actors = {no: (actor if no == 1 else FlakySpeakActor(0)) for no in SEATS}
    with pytest.raises(RuntimeError, match="programming bug"):
        await run(
            DummyRules(),
            setup=None,
            actors=actors,
            journal=NullJournal(),
            threads=_threads(),
        )
    assert actor.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["apply", "journal"])
async def test_expectation_rolls_back_all_actors_before_commit(failure: str):
    events: list[str] = []

    class TrackingActor:
        def __init__(self, seat: int) -> None:
            self.seat = seat
            self.rollbacks = 0
            self.commits = 0

        async def speak(self, seat: int, view: Any, turn_id: str) -> str:
            return f"{seat}号说：大家好"

        def commit_turn(self, seat: int) -> None:
            self.commits += 1
            events.append(f"commit:{seat}")

        async def rollback(self, seat: int) -> None:
            self.rollbacks += 1

    class ApplyFailureRules(OneTurnTwoSeatRules):
        def apply(self, state: dict, action: str) -> dict:
            if failure == "apply":
                raise RuntimeError("apply failed")
            return super().apply(state, action)

    class RecordingJournal(NullJournal):
        def append_batch(self, sents):
            events.append("journal")
            if failure == "journal":
                raise OSError("journal failed")
            return super().append_batch(sents)

    actors = {seat: TrackingActor(seat) for seat in (1, 2)}
    expected_error = RuntimeError if failure == "apply" else OSError
    with pytest.raises(expected_error):
        await run(
            ApplyFailureRules(),
            setup=None,
            actors=actors,
            journal=RecordingJournal(),
            threads={1: SeatThread(), 2: SeatThread()},
        )

    assert [actors[seat].rollbacks for seat in (1, 2)] == [1, 1]
    assert [actors[seat].commits for seat in (1, 2)] == [0, 0]
    assert not any(event.startswith("commit:") for event in events)


@pytest.mark.asyncio
async def test_actor_commit_happens_only_after_journal_commit():
    events: list[str] = []

    class TrackingActor(FlakySpeakActor):
        def commit_turn(self, seat: int) -> None:
            events.append(f"commit:{seat}")

    class RecordingJournal(NullJournal):
        def append_batch(self, sents):
            events.append("journal")
            return super().append_batch(sents)

    await run(
        OneTurnTwoSeatRules(),
        setup=None,
        actors={1: TrackingActor(0), 2: TrackingActor(0)},
        journal=RecordingJournal(),
        threads={1: SeatThread(), 2: SeatThread()},
    )
    assert events == ["journal", "commit:1", "commit:2"]


@pytest.mark.asyncio
async def test_retry_backoff_does_not_hold_concurrency_slot(monkeypatch):
    sleeping = asyncio.Event()
    release_sleep = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        sleeping.set()
        await release_sleep.wait()

    monkeypatch.setattr(loop_mod, "_sleep", controlled_sleep)

    class RetryingActor(FlakySpeakActor):
        pass

    class ObservedActor(FlakySpeakActor):
        def __init__(self) -> None:
            super().__init__(0)
            self.spoke = asyncio.Event()

        async def speak(self, seat: int, view: Any, turn_id: str) -> str:
            self.spoke.set()
            return await super().speak(seat, view, turn_id)

    retrying = RetryingActor(1)
    observed = ObservedActor()
    task = asyncio.create_task(
        run(
            OneTurnTwoSeatRules(),
            setup=None,
            actors={1: retrying, 2: observed},
            journal=NullJournal(),
            threads={1: SeatThread(), 2: SeatThread()},
            max_concurrency=1,
        )
    )
    await asyncio.wait_for(sleeping.wait(), timeout=1)
    await asyncio.wait_for(observed.spoke.wait(), timeout=1)
    release_sleep.set()
    await task


@pytest.mark.asyncio
async def test_concurrent_actor_failure_cancels_and_awaits_siblings():
    class TwoActorRules(DummyRules):
        def expected(self, state: dict) -> Expectation:
            return Expectation(seats=[1, 2], turn_id="concurrent.0")

        def pending(self, state: dict):
            return []

    class BlockingActor:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.rollbacks = 0

        async def speak(self, seat: int, view: Any, turn_id: str):
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        def commit_turn(self, seat: int) -> None: ...

        async def rollback(self, seat: int) -> None:
            self.rollbacks += 1

    class FatalActor:
        def __init__(self, sibling_started: asyncio.Event) -> None:
            self.sibling_started = sibling_started

        async def speak(self, seat: int, view: Any, turn_id: str):
            await self.sibling_started.wait()
            raise FatalActorError("session poisoned")

        def commit_turn(self, seat: int) -> None: ...
        async def rollback(self, seat: int) -> None: ...

    blocker = BlockingActor()
    actors = {1: FatalActor(blocker.started), 2: blocker}
    threads = {1: SeatThread(), 2: SeatThread()}

    with pytest.raises(FatalActorError, match="poisoned"):
        await run(
            TwoActorRules(),
            setup=None,
            actors=actors,
            journal=NullJournal(),
            threads=threads,
        )

    assert blocker.cancelled.is_set()
    assert blocker.rollbacks >= 1
