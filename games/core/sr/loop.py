"""句子运行时的唯一驱动循环。

rules 决定语义，loop 只做 actor 调用、turn 事务提交、路由与确定性重放。
每个 expectation 的所有玩家句子和由它们产生的 host 句子共享一个原子
``commit_id``；日志提交成功后才对 thread 和 renderer 发布该批。
"""

from __future__ import annotations

import asyncio
import random
import sys
from typing import Any, Protocol

from pydantic import ValidationError

from core.sr.errors import FatalActorError
from core.sr.journal import Journal
from core.sr.rules import Rules
from core.sr.types import Delivery, Expectation, HostEmit, ParseResult, Renderer, Sentence
from core.threads import SeatThread, ThreadMessage


class Actor(Protocol):
    """一个席位的说话者契约。

    commit_turn / rollback 是“产出验收”回执：rules.parse 过才 commit，不过就
    rollback，避免坏产出污染 actor 会话。无会话 actor 的两个方法都是空操作。
    """

    async def speak(self, seat: int, view: Any, turn_id: str) -> tuple[str, dict[str, Any] | None]: ...

    def commit_turn(self, seat: int) -> None: ...

    async def rollback(self, seat: int) -> None: ...

    async def close(self) -> None: ...


def _validate_seat_keys(mapping: dict[int, Any], name: str) -> None:
    """在运行边界一次性收紧 seat 世界；下游直接按键访问。"""
    if not mapping:
        raise RuntimeError(f"{name} must contain at least one seat")
    invalid = [seat for seat in mapping if type(seat) is not int or seat <= 0]
    if invalid:
        raise RuntimeError(f"{name} contains invalid seat keys: {invalid!r}")


def _validate_live_world(actors: dict[int, Actor], threads: dict[int, SeatThread]) -> None:
    _validate_seat_keys(actors, "actors")
    _validate_seat_keys(threads, "threads")
    if actors.keys() != threads.keys():
        raise RuntimeError(
            "actors and threads must describe the same seats: "
            f"actors={sorted(actors)}, threads={sorted(threads)}"
        )


def _validated_expectation(
    expectation: Expectation,
    threads: dict[int, SeatThread],
    actors: dict[int, Actor] | None = None,
) -> Expectation:
    try:
        checked = Expectation.model_validate(expectation.model_dump(), strict=True)
    except (AttributeError, ValidationError) as exc:
        raise RuntimeError("rules.expected returned a malformed Expectation") from exc
    missing_threads = [seat for seat in checked.seats if seat not in threads]
    if missing_threads:
        raise RuntimeError(f"expectation references seats without threads: {missing_threads}")
    if actors is not None:
        missing_actors = [seat for seat in checked.seats if seat not in actors]
        if missing_actors:
            raise RuntimeError(f"expectation references seats without actors: {missing_actors}")
    return checked


def _validated_deliveries(
    deliveries: list[Delivery],
    threads: dict[int, SeatThread],
) -> list[Delivery]:
    """校验 Delivery 自身形状及其对当前 seat 世界的全部引用。"""
    checked: list[Delivery] = []
    for index, delivery in enumerate(deliveries):
        try:
            item = Delivery.model_validate(delivery.model_dump(), strict=True)
        except (AttributeError, ValidationError) as exc:
            raise RuntimeError(f"delivery {index} is malformed") from exc
        missing = [
            channel
            for channel in item.channels
            if type(channel) is int and channel not in threads
        ]
        if missing:
            raise RuntimeError(f"delivery {index} references unknown seats: {missing}")
        checked.append(item)
    return checked


def _validate_sentence_batch(
    sentences: list[Sentence],
    threads: dict[int, SeatThread],
) -> list[Sentence]:
    """提交前校验整批引用，确保 journal 永远不会先写入毒路由。"""
    checked: list[Sentence] = []
    host_seen = False
    for index, sentence in enumerate(sentences):
        try:
            item = Sentence.model_validate(sentence.model_dump(), strict=True)
        except (AttributeError, ValidationError) as exc:
            raise RuntimeError(f"sentence {index} is malformed") from exc
        if item.speaker == "host":
            host_seen = True
        else:
            if host_seen:
                raise RuntimeError("player sentence cannot follow a host sentence in one commit")
            if item.speaker not in threads:
                raise RuntimeError(f"sentence {index} references unknown speaker: {item.speaker}")
        _validated_deliveries(item.deliveries, threads)
        checked.append(item)
    return checked


def _validate_logged_batches(
    batches: list[list[dict[str, Any]]],
    threads: dict[int, SeatThread],
) -> list[list[Sentence]]:
    """恢复前一次性验完整份日志形状与路由引用，不留下半恢复 thread。"""
    checked_batches: list[list[Sentence]] = []
    for batch in batches:
        commit_id = batch[0].get("commit_id") if batch else "?"
        try:
            sentences = [Sentence.model_validate(row, strict=True) for row in batch]
            checked_batches.append(_validate_sentence_batch(sentences, threads))
        except (ValidationError, RuntimeError) as exc:
            raise RuntimeError(
                f"日志 commit {commit_id} 的 Sentence 或路由不合法：{exc}"
            ) from exc
    return checked_batches


def _deliver_validated(deliveries: list[Delivery], threads: dict[int, SeatThread]) -> None:
    for delivery in deliveries:
        if not delivery.text:
            continue
        for channel in delivery.channels:
            if channel == "broadcast":
                for thread in threads.values():
                    thread.inbox.append(delivery.text)
            elif channel == "audience":
                pass
            else:
                threads[channel].inbox.append(delivery.text)


def deliver(deliveries: list[Delivery], threads: dict[int, SeatThread]) -> None:
    """在公开边界校验后按 channel 机械路由 Delivery。"""
    _deliver_validated(_validated_deliveries(deliveries, threads), threads)


def _publish_batch(sentences: list[Sentence], threads: dict[int, SeatThread]) -> None:
    """发布完整事务；同时行动者先一起清 inbox，再按物理顺序收齐全部发言。"""
    players = [sentence for sentence in sentences if sentence.speaker != "host"]
    hosts = [sentence for sentence in sentences if sentence.speaker == "host"]
    for sentence in players:
        thread = threads[sentence.speaker]
        thread.inbox.clear()
        thread.messages.append(ThreadMessage(role="human", content=sentence.prompt))
        thread.messages.append(ThreadMessage(role="assistant", content=sentence.raw))
    for sentence in [*players, *hosts]:
        _deliver_validated(sentence.deliveries, threads)


def _take_pending(rules: Rules, state: Any, seen_turns: set[str]) -> list[HostEmit]:
    """按 live 契约取尚未落过日志的 host emit，并立刻占用其 turn_id。"""
    pending: list[HostEmit] = []
    for emit in rules.pending(state):
        if emit.turn_id in seen_turns:
            continue
        pending.append(emit)
        seen_turns.add(emit.turn_id)
    return pending


def _host_sentence(emit: HostEmit) -> Sentence:
    return Sentence(
        seq=-1,
        turn_id=emit.turn_id,
        speaker="host",
        deliveries=emit.deliveries,
        action=emit.action,
    )


def _delivery_rows(parsed: ParseResult) -> list[dict[str, Any]]:
    return [delivery.model_dump() for delivery in parsed.deliveries]


def _assert_player_row(
    rules: Rules,
    state: Any,
    thread: SeatThread,
    row: dict[str, Any],
    *,
    seat: int,
    turn_id: str,
    commit_id: int,
) -> None:
    """验证玩家日志确实是当前 state 下 parse(raw) 的确定产物。"""
    if type(row.get("speaker")) is not int or row["speaker"] != seat:
        raise RuntimeError(
            f"日志 turn {turn_id} 席位不完整或次序错误：期望 {seat}，"
            f"commit {commit_id} 实际 {row.get('speaker')!r}"
        )
    if row.get("turn_id") != turn_id:
        raise RuntimeError(
            f"日志与规则机失步：state 期望 turn {turn_id}，"
            f"commit {commit_id} 是 {row.get('turn_id')!r}"
        )
    if not isinstance(row.get("raw"), str) or not isinstance(row.get("prompt"), str):
        raise RuntimeError(f"日志 commit {commit_id} 的玩家 {seat} raw/prompt 不是字符串")
    try:
        view = rules.view(seat, state, thread, first_speak=False)
    except Exception as exc:
        raise RuntimeError(f"日志 commit {commit_id} 的玩家 {seat} 已无法重建行动视图") from exc
    turn_prompt = getattr(view, "turn_prompt", None)
    if not isinstance(turn_prompt, str) or row["prompt"] != turn_prompt:
        raise RuntimeError(f"日志 commit {commit_id} 的玩家 {seat} prompt 与规则视图不一致")
    try:
        parsed = rules.parse(seat, row["raw"], state)
    except Exception as exc:
        raise RuntimeError(f"日志 commit {commit_id} 的玩家 {seat} raw 已无法通过规则解析") from exc
    if row.get("action") != parsed.action or row.get("deliveries") != _delivery_rows(parsed):
        raise RuntimeError(
            f"日志 commit {commit_id} 的玩家 {seat} action/deliveries 与 parse(raw) 不一致"
        )


def _assert_host_rows(
    rows: list[dict[str, Any]],
    emits: list[HostEmit],
    *,
    commit_id: int,
) -> None:
    """精确验证 host 的数量、顺序、路由文本与 opaque action。"""
    expected = [
        {
            "turn_id": emit.turn_id,
            "speaker": "host",
            "raw": "",
            "prompt": "",
            "deliveries": [delivery.model_dump() for delivery in emit.deliveries],
            "action": emit.action,
        }
        for emit in emits
    ]
    actual = [
        {
            "turn_id": row.get("turn_id"),
            "speaker": row.get("speaker"),
            "raw": row.get("raw"),
            "prompt": row.get("prompt"),
            "deliveries": row.get("deliveries"),
            "action": row.get("action"),
        }
        for row in rows
    ]
    if actual != expected:
        raise RuntimeError(
            f"日志 commit {commit_id} 的 host 句子缺失、多余、乱序或内容不一致："
            f"期望 {[emit.turn_id for emit in emits]}，"
            f"实际 {[row.get('turn_id') for row in rows]}"
        )


_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0


async def _gather_cancel_on_error(coroutines: list[Any]) -> list[Any]:
    """任一并发 actor 失败时，取消并等待全部 sibling 后再传播原异常。"""
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def run(
    rules: Rules,
    setup: Any,
    actors: dict[int, Actor],
    journal: Journal,
    threads: dict[int, SeatThread],
    *,
    renderer: Renderer | None = None,
    snapshot_every: int = 0,
    state: Any = None,
    max_concurrency: int = 0,
) -> Any:
    """跑一整局到终局，返回终局 state。

    ``snapshot_every > 0`` 时在 turn 边界节流保存 state；0 表示只存终态。
    ``max_concurrency > 0`` 限制同时 actor 调用数；0 不限。

    本函数采用 fail-stop 语义：任一 actor、journal 或 renderer 异常都会终止本次运行。
    调用方不得捕获后原地复用同一组 actors 继续；应关闭它们，并从已提交 journal
    通过 ``resume`` 建立全新 actor session。
    """
    _validate_live_world(actors, threads)
    if state is None:
        state = rules.initial_state(setup)
    last_seq = journal.last_seq()
    spoken: set[int] = set()
    existing_batches = journal.load_batches()
    _validate_logged_batches(existing_batches, threads)
    journaled_host_turns = {
        row["turn_id"]
        for batch_rows in existing_batches
        for row in batch_rows
        if row["speaker"] == "host"
    }

    def _collect_host(buffer: list[Sentence]) -> None:
        """把当前 state 的 host emit 收进本 turn，此时不路由、不渲染。"""
        buffer.extend(
            _host_sentence(emit)
            for emit in _take_pending(rules, state, journaled_host_turns)
        )

    def _commit_and_publish(buffer: list[Sentence]) -> None:
        nonlocal last_seq
        if not buffer:
            return
        _validate_sentence_batch(buffer, threads)
        last_seq = journal.append_batch(buffer)
        # 先完整恢复 thread；renderer 异常也不会留下半批 inbox。
        _publish_batch(buffer, threads)
        spoken.update(
            sentence.speaker for sentence in buffer if sentence.speaker != "host"
        )
        # append_batch 已分配 seq/commit_id，renderer 永远看到可定位的句子。
        if renderer is not None:
            for sentence in buffer:
                renderer.render_sentence(sentence)
        buffer.clear()

    batch: list[Sentence] = []
    _collect_host(batch)
    _commit_and_publish(batch)

    semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None

    while not rules.is_terminal(state):
        expectation = _validated_expectation(rules.expected(state), threads, actors)

        async def _ask(
            seat: int,
            _state: Any = state,
            _turn_id: str = expectation.turn_id,
        ) -> tuple[int, str, dict[str, Any] | None, Any, str]:
            view = rules.view(seat, _state, threads[seat], first_speak=seat not in spoken)
            turn_prompt = getattr(view, "turn_prompt", None)
            if not isinstance(turn_prompt, str):
                raise TypeError(f"rules.view result.turn_prompt must be str, got {type(turn_prompt)}")
            if semaphore is None:
                raw, audit, parsed = await _act_with_retry(
                    rules, actors[seat], seat, view, _state, _turn_id
                )
            else:
                async with semaphore:
                    raw, audit, parsed = await _act_with_retry(
                        rules, actors[seat], seat, view, _state, _turn_id
                    )
            return seat, raw, audit, parsed, turn_prompt

        gathered = await _gather_cancel_on_error(
            [_ask(seat) for seat in expectation.seats]
        )

        for seat, raw, _audit, parsed, turn_prompt in gathered:
            batch.append(
                Sentence(
                    seq=-1,
                    turn_id=expectation.turn_id,
                    speaker=seat,
                    raw=raw,
                    prompt=turn_prompt,
                    deliveries=parsed.deliveries,
                    action=parsed.action,
                )
            )
            state = rules.apply(state, parsed.action)

        _collect_host(batch)
        state = rules.advance(state)
        _collect_host(batch)
        _commit_and_publish(batch)

        if snapshot_every and (last_seq + 1) % snapshot_every == 0:
            journal.save_snapshot(last_seq, rules.pack(state))

    journal.save_snapshot(last_seq, rules.pack(state))
    return state


def _replay_player_batch(
    rules: Rules,
    state: Any,
    batch: list[dict[str, Any]],
    seen_host_turns: set[str],
    threads: dict[int, SeatThread],
) -> Any:
    """验证并 fold 一个 live loop 产出的完整玩家事务。"""
    commit_id = batch[0]["commit_id"]
    if rules.is_terminal(state):
        raise RuntimeError(f"日志在终态之后仍有句子（commit {commit_id}）")

    expectation = _validated_expectation(rules.expected(state), threads)
    player_count = len(expectation.seats)
    if len(batch) < player_count:
        raise RuntimeError(
            f"日志 turn {expectation.turn_id} 席位不完整或次序错误："
            f"期望 {expectation.seats}，commit {commit_id} 只有 {len(batch)} 条"
        )

    turn_state = state
    player_rows = batch[:player_count]
    for seat, row in zip(expectation.seats, player_rows, strict=True):
        _assert_player_row(
            rules,
            turn_state,
            threads[seat],
            row,
            seat=seat,
            turn_id=expectation.turn_id,
            commit_id=commit_id,
        )

    for row in player_rows:
        state = rules.apply(state, row["action"])
    host_emits = _take_pending(rules, state, seen_host_turns)
    state = rules.advance(state)
    host_emits.extend(_take_pending(rules, state, seen_host_turns))
    _assert_host_rows(batch[player_count:], host_emits, commit_id=commit_id)
    return state


async def resume(
    rules: Rules,
    actors: dict[int, Actor],
    journal: Journal,
    threads: dict[int, SeatThread],
    *,
    setup: Any = None,
) -> Any:
    """从完整 commit 日志恢复 state 和每席位的完整私有 thread。

    thread 从全部 Sentence 线性重建；state 也必须逐批 fold 全日志，以验证每个
    commit 当时的 expected seats/turn_id。快照不能跳过这层逻辑校验，否则一份与
    物理日志哈希相符、但内容语义不完整的快照会让 state/thread 分裂。当前快照仅是
    原子保存且与日志绑定的派生产物，不参与恢复真相；将来若要用它加速，必须先为
    被跳过的 commit 提供等价的可验证证明。
    """
    del actors  # resume 是纯重放，不调 actor。
    batches = journal.load_batches()
    _validate_seat_keys(threads, "threads")
    checked_batches = _validate_logged_batches(batches, threads)

    for thread in threads.values():
        thread.inbox.clear()
        thread.messages.clear()
    state = rules.initial_state(setup)
    seen_host_turns: set[str] = set()
    batch_index = 0

    # live loop 在首个 expectation 之前单独提交 initial pending。日志为空表示
    # 进程可能恰好在这次提交前崩溃，属于合法未开始状态；若已有后续 batch 则不能跳过。
    initial_emits = _take_pending(rules, state, seen_host_turns)
    if initial_emits and batches:
        first = batches[0]
        _assert_host_rows(first, initial_emits, commit_id=first[0]["commit_id"])
        _publish_batch(checked_batches[0], threads)
        batch_index = 1

    for index in range(batch_index, len(batches)):
        batch = batches[index]
        state = _replay_player_batch(rules, state, batch, seen_host_turns, threads)
        _publish_batch(checked_batches[index], threads)
    return state


async def _act_with_retry(
    rules: Rules,
    actor: Actor,
    seat: int,
    view: Any,
    state: Any,
    turn_id: str,
) -> tuple[str, dict[str, Any] | None, Any]:
    """调 actor + rules.parse，作为一个单元带退避重试。"""
    for retry_no in range(1, _MAX_RETRIES + 1):
        try:
            try:
                raw, audit = await actor.speak(seat, view, turn_id)
                try:
                    parsed = rules.parse(seat, raw, state)
                except Exception:
                    await actor.rollback(seat)
                    raise
                actor.commit_turn(seat)
                return raw, audit, parsed
            except asyncio.CancelledError:
                await actor.rollback(seat)
                raise
        except FatalActorError:
            raise
        except Exception as exc:
            if retry_no == _MAX_RETRIES:
                raise
            delay = min(_BACKOFF_BASE * 2 ** (retry_no - 1), _BACKOFF_CAP) + random.random()
            print(
                f"[retry] 座位{seat} turn={turn_id} 第 {retry_no}/{_MAX_RETRIES} 次失败，"
                f"{delay:.1f}s 后重试：{exc}",
                file=sys.stderr,
            )
            await _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def _sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def run_async_main[T](coroutine) -> T:  # type: ignore[no-untyped-def]
    """用 asyncio.Runner 运行 CLI 协程，并把首次 SIGINT 转成友好取消。"""
    import signal

    with asyncio.Runner() as runner:
        loop = runner.get_loop()
        task = loop.create_task(coroutine)
        interrupted = False
        previous = signal.getsignal(signal.SIGINT)

        def handle_sigint() -> None:
            nonlocal interrupted
            if interrupted:
                return
            interrupted = True
            task.cancel()

        loop.add_signal_handler(signal.SIGINT, handle_sigint)
        try:
            try:
                return loop.run_until_complete(task)
            except asyncio.CancelledError as exc:
                raise KeyboardInterrupt from exc
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous)
