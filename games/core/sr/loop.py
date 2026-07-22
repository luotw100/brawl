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

from core.parsing import JsonParseError
from core.sr.errors import FatalActorError, RetryableActorError
from core.sr.journal import Journal
from core.sr.rules import Rules
from core.sr.types import Delivery, Expectation, HostEmit, ParseResult, Renderer, Sentence
from core.threads import SeatThread, ThreadMessage


class Actor(Protocol):
    """一个席位的说话者契约。

    commit_turn / rollback 是 turn 事务回执：整个 expectation 已落 journal 才
    commit；任一 sibling、parse、apply 或 journal 失败都 rollback 全体行动者。
    """

    async def speak(self, seat: int, view: Any, turn_id: str) -> str: ...

    def commit_turn(self, seat: int) -> None: ...

    async def rollback(self, seat: int) -> None: ...


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


def _validated_pending(
    rules: Rules,
    state: Any,
    seen_turns: set[str],
) -> tuple[list[HostEmit], set[str]]:
    """在 host 边界验完整批次；turn_id 任何重复都大声失败。"""
    raw = rules.pending(state)
    if not isinstance(raw, list):
        raise RuntimeError("rules.pending must return a list")
    checked: list[HostEmit] = []
    updated = set(seen_turns)
    for index, emit in enumerate(raw):
        try:
            item = HostEmit.model_validate(emit.model_dump(), strict=True)
        except (AttributeError, ValidationError) as exc:
            raise RuntimeError(f"rules.pending returned malformed HostEmit at {index}") from exc
        if item.turn_id in updated:
            raise RuntimeError(f"host turn_id 重复：{item.turn_id}")
        updated.add(item.turn_id)
        checked.append(item)
    return checked, updated


def _logged_host_turns(batches: list[list[dict[str, Any]]]) -> set[str]:
    seen: set[str] = set()
    for batch in batches:
        for row in batch:
            if row["speaker"] != "host":
                continue
            turn_id = row["turn_id"]
            if turn_id in seen:
                raise RuntimeError(f"日志中 host turn_id 重复：{turn_id}")
            seen.add(turn_id)
    return seen


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


async def _rollback_turn(actors: dict[int, Actor], seats: list[int]) -> None:
    """并发回滚整个 expectation；任何回滚失败都意味着 actor 已不可再用。"""
    results = await asyncio.gather(
        *(actors[seat].rollback(seat) for seat in seats),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise FatalActorError("expectation rollback 未能完整完成") from failures[0]


async def run(
    rules: Rules,
    setup: Any,
    actors: dict[int, Actor],
    journal: Journal,
    threads: dict[int, SeatThread],
    *,
    renderer: Renderer | None = None,
    max_concurrency: int = 0,
) -> Any:
    """跑一整局到终局，返回终局 state。

    ``max_concurrency > 0`` 限制同时 actor 调用数；0 不限。journal 非空时，
    本函数亲自严格重放出 state/thread 后再继续，不接受调用方提供的裸状态。

    本函数采用 fail-stop 语义：任一 actor、journal 或 renderer 异常都会终止本次运行。
    调用方不得捕获后原地复用同一组 actors 继续；应关闭它们，并从已提交 journal
    通过 ``resume`` 建立全新 actor session。
    """
    _validate_live_world(actors, threads)
    existing_batches = journal.load_batches()
    if existing_batches:
        state = await resume(rules, journal, threads, setup=setup)
    else:
        state = rules.initial_state(setup)
    spoken: set[int] = set()
    journaled_host_turns = _logged_host_turns(existing_batches)

    def _publish_committed(buffer: list[Sentence]) -> None:
        """journal 已是真相后，机械发布到 thread 和 renderer。"""
        _publish_batch(buffer, threads)
        spoken.update(
            sentence.speaker for sentence in buffer if sentence.speaker != "host"
        )
        if renderer is not None:
            for sentence in buffer:
                renderer.render_sentence(sentence)

    # 非空 journal 的 state 已由 resume 重放到当前边界，那一刻的 pending 也已在
    # 日志中；空日志（包括 initial host 提交前崩溃）则仍需生成开场。
    if not existing_batches:
        initial_emits, next_seen = _validated_pending(rules, state, journaled_host_turns)
        initial_batch = [_host_sentence(emit) for emit in initial_emits]
        if initial_batch:
            _validate_sentence_batch(initial_batch, threads)
            journal.append_batch(initial_batch)
            journaled_host_turns = next_seen
            _publish_committed(initial_batch)

    semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None

    while not rules.is_terminal(state):
        expectation = _validated_expectation(rules.expected(state), threads, actors)

        async def _ask(
            seat: int,
            _state: Any = state,
            _turn_id: str = expectation.turn_id,
        ) -> tuple[int, str, Any, str]:
            view = rules.view(seat, _state, threads[seat], first_speak=seat not in spoken)
            turn_prompt = getattr(view, "turn_prompt", None)
            if not isinstance(turn_prompt, str):
                raise TypeError(f"rules.view result.turn_prompt must be str, got {type(turn_prompt)}")
            raw, parsed = await _act_with_retry(
                rules,
                actors[seat],
                seat,
                view,
                _state,
                _turn_id,
                semaphore,
            )
            return seat, raw, parsed, turn_prompt

        try:
            gathered = await _gather_cancel_on_error(
                [_ask(seat) for seat in expectation.seats]
            )

            candidate_state = state
            batch: list[Sentence] = []
            for seat, raw, parsed, turn_prompt in gathered:
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
                candidate_state = rules.apply(candidate_state, parsed.action)

            emits, candidate_seen = _validated_pending(
                rules,
                candidate_state,
                journaled_host_turns,
            )
            batch.extend(_host_sentence(emit) for emit in emits)
            candidate_state = rules.advance(candidate_state)
            emits, candidate_seen = _validated_pending(
                rules,
                candidate_state,
                candidate_seen,
            )
            batch.extend(_host_sentence(emit) for emit in emits)

            _validate_sentence_batch(batch, threads)
            journal.append_batch(batch)
        except BaseException as exc:
            try:
                await _rollback_turn(actors, expectation.seats)
            except BaseException as rollback_exc:
                raise rollback_exc from exc
            raise

        # 至此 journal 已经是唯一真相；actor session 才允许保留本轮消息。
        state = candidate_state
        journaled_host_turns = candidate_seen
        for seat in expectation.seats:
            actors[seat].commit_turn(seat)
        _publish_committed(batch)

    return state


def _replay_player_batch(
    rules: Rules,
    state: Any,
    batch: list[dict[str, Any]],
    seen_host_turns: set[str],
    threads: dict[int, SeatThread],
) -> tuple[Any, set[str]]:
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
    host_emits, next_seen = _validated_pending(rules, state, seen_host_turns)
    state = rules.advance(state)
    more_emits, next_seen = _validated_pending(rules, state, next_seen)
    host_emits.extend(more_emits)
    _assert_host_rows(batch[player_count:], host_emits, commit_id=commit_id)
    return state, next_seen


async def resume(
    rules: Rules,
    journal: Journal,
    threads: dict[int, SeatThread],
    *,
    setup: Any = None,
) -> Any:
    """从完整 commit 日志恢复 state 和每席位的完整私有 thread。

    thread 从全部 Sentence 线性重建；state 也逐批 fold 全日志，以验证每个 commit
    当时的 expected seats/turn_id。恢复先在隔离的 thread 副本中完成，整份日志通过
    后才一次发布给调用者，错误日志不会留下半恢复内存状态。
    """
    batches = journal.load_batches()
    _validate_seat_keys(threads, "threads")
    checked_batches = _validate_logged_batches(batches, threads)
    _logged_host_turns(batches)

    working_threads = {seat: SeatThread() for seat in threads}
    state = rules.initial_state(setup)
    seen_host_turns: set[str] = set()
    batch_index = 0

    # live loop 在首个 expectation 之前单独提交 initial pending。日志为空表示
    # 进程可能恰好在这次提交前崩溃，属于合法未开始状态；若已有后续 batch 则不能跳过。
    initial_emits, initial_seen = _validated_pending(rules, state, seen_host_turns)
    if initial_emits and batches:
        first = batches[0]
        _assert_host_rows(first, initial_emits, commit_id=first[0]["commit_id"])
        _publish_batch(checked_batches[0], working_threads)
        seen_host_turns = initial_seen
        batch_index = 1

    for index in range(batch_index, len(batches)):
        batch = batches[index]
        state, seen_host_turns = _replay_player_batch(
            rules,
            state,
            batch,
            seen_host_turns,
            working_threads,
        )
        _publish_batch(checked_batches[index], working_threads)

    for seat, restored in working_threads.items():
        threads[seat].inbox[:] = restored.inbox
        threads[seat].messages[:] = restored.messages
    return state


async def _act_with_retry(
    rules: Rules,
    actor: Actor,
    seat: int,
    view: Any,
    state: Any,
    turn_id: str,
    semaphore: asyncio.Semaphore | None,
) -> tuple[str, Any]:
    """只重试明确的瞬态 actor 失败与明确的模型产出拒绝。"""
    for retry_no in range(1, _MAX_RETRIES + 1):
        retry_error: BaseException
        try:
            if semaphore is None:
                raw = await actor.speak(seat, view, turn_id)
            else:
                async with semaphore:
                    raw = await actor.speak(seat, view, turn_id)
        except asyncio.CancelledError:
            await actor.rollback(seat)
            raise
        except RetryableActorError as exc:
            await actor.rollback(seat)
            retry_error = exc
        except BaseException:
            await actor.rollback(seat)
            raise
        else:
            try:
                parsed = rules.parse(seat, raw, state)
            except JsonParseError as exc:
                await actor.rollback(seat)
                retry_error = exc
            except BaseException:
                await actor.rollback(seat)
                raise
            else:
                return raw, parsed

        if retry_no == _MAX_RETRIES:
            raise retry_error
        delay = min(_BACKOFF_BASE * 2 ** (retry_no - 1), _BACKOFF_CAP) + random.random()
        print(
            f"[retry] 座位{seat} turn={turn_id} 第 {retry_no}/{_MAX_RETRIES} 次失败，"
            f"{delay:.1f}s 后重试：{retry_error}",
            file=sys.stderr,
        )
        # semaphore 只包实际 speak；回滚与退避都不会占用并发槽。
        await _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def _sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def run_async_main[T](coroutine) -> T:  # type: ignore[no-untyped-def]
    """运行 CLI 协程；Runner 原生提供首次取消、再次 SIGINT 强中断。"""
    with asyncio.Runner() as runner:
        return runner.run(coroutine)
