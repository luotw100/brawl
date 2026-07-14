"""驱动循环：整个引擎。替代 LangGraph 的 astream + 节点编排。

语义全在 rules，管道全在 loop：

    state = rules.initial_state(setup)
    while not rules.is_terminal(state):
        exp     = rules.expected(state)
        raw, ll = await actor.speak(exp.seat, exp.view)    # opencode 调用，返回原文 + 审计行
        pr      = rules.parse(raw, state, exp)             # 唯一懂语义；不过 raise 重试
        seq     = journal.next_seq()
        sent    = Sentence(seq, exp.turn_id, exp.seat, raw, pr.deliveries, pr.action)
        journal.append_sentence(sent, ll)
        deliver(pr.deliveries, threads)                    # 唯一懂路由
        state = rules.advance(rules.apply(state, pr.action))

句子日志即 checkpoint：resume 时从 snapshot 重放 sentences，回到崩溃前状态。
没有两路流对齐，没有 sidecar/watermark，没有 torn 超步。
"""

from __future__ import annotations

import random
import sys
from typing import Any, Protocol

from core.sr.journal import Journal
from core.sr.rules import Rules
from core.sr.types import Delivery, Sentence
from core.threads import SeatThread


class Actor(Protocol):
    """一个席位的说话者契约。core 用它替代旧的 ModelBackend + AgentActor 两层。

    rules.view 产出的 prompt 文本原样进来；actor 原样发给该 seat 的 opencode/session，
    拿回 (raw, audit)。audit 是写到 llm_calls.jsonl 的一行字段，形状由 actor 自定
    （OpenCodeActor 复用旧格式，RandomActor 填等价字段）。author 字段是 seq+turn_id。
    """

    async def speak(self, seat: int, prompt: str, turn_id: str, seq: int) -> tuple[str, dict[str, Any] | None]: ...

    async def close(self) -> None: ...


def deliver(deliveries: list[Delivery], threads: dict[int, SeatThread]) -> None:
    """按 channel 写 inbox。core 唯一懂路由的活。

    - int    -> threads[seat].inbox.append(text)
    - "broadcast" -> 全员 inbox
    - "audience" -> 不进任何 seat（由 journal 的 sentence 记录承载，stage/TTS 读它）
    """
    for d in deliveries:
        if not d.text:
            continue
        for ch in d.channels:
            if ch == "broadcast":
                for t in threads.values():
                    t.inbox.append(d.text)
            elif ch == "audience":
                pass  # 观众流由 journal 的 sentence 行承载
            else:
                threads[ch].inbox.append(d.text)


_MAX_ATTEMPTS = 5
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0


async def run(
    rules: Rules,
    setup: Any,
    actors: dict[int, Actor],
    journal: Journal,
    threads: dict[int, SeatThread],
    *,
    snapshot_every: int = 0,
) -> Any:
    """跑一整局到终局，返回终局 state。

    `snapshot_every > 0` 时每 N 条 sentence 存一次 state 快照加速 resume；0 = 不存（靠重放）。
    """
    state = rules.initial_state(setup)
    seq = -1
    while not rules.is_terminal(state):
        exp = rules.expected(state)
        seat = exp.seat
        actor = actors.get(seat) if isinstance(seat, int) else None
        if actor is None:
            # host 串场词等非玩家 expectation：rules 应已用 action/deliveries 给出，无需 actor。
            # 为 KISS，host 由 rules 在 advance/apply 内自行产生并 emit（见 DummyRules），
            # loop 不对 host 调 actor。到此分支意味着玩法把 host 放进 expected 却没在 rules 内消化。
            raise RuntimeError(f"no actor for seat={seat!r}; host sentences must not go through actors")

        seq = journal.next_seq()
        raw, audit = await _speak_with_retry(actor, seat, exp.view, exp.turn_id, seq)
        pr = rules.parse(raw, state, exp)  # 不过 -> ValueError，重试语义见文档风险项 2

        sent = Sentence(seq=seq, turn_id=exp.turn_id, speaker=seat, raw=raw, deliveries=pr.deliveries, action=pr.action)
        journal.append_sentence(sent, audit)
        deliver(pr.deliveries, threads)
        state = rules.advance(rules.apply(state, pr.action))

        if snapshot_every and seq % snapshot_every == 0:
            journal.save_snapshot(seq, rules.pack(state))

    # 终局存一份终态 snapshot，标记'全部 sentences 已 apply'（seq=-1 表示零句即终局）。
    journal.save_snapshot(seq, rules.pack(state))
    return state


async def resume(
    rules: Rules,
    actors: dict[int, Actor],
    journal: Journal,
    threads: dict[int, SeatThread],
    *,
    setup: Any = None,
) -> Any:
    """从崩溃点恢复，回到崩溃前 state，不调 actor。

    优先用 snapshot（快）：unpack 后从 `seq+1` 重放剩余 sentences 到最新终态。无 snapshot
    时从 `setup` 造 initial_state 再重放全部 sentences（慢路径）——两种路径对 `apply` 的
    要求一致，故结果与实时跑等价（回放确定性不变量）。

    `setup` 由玩法侧从 meta.json 重建后传入；冷续跑（无 snapshot）时必需。
    """
    snap = journal.latest_snapshot()
    recorded = journal.load_sentences()
    if snap is None:
        # 冷续跑：从 setup 重建 initial_state 再重放全部 sentences。setup 是否必需、None
        # 合不合法由 rules 决定——core 不替 rules 判断"None 是否意味着缺失"。
        state = rules.initial_state(setup)
        start_idx = 0
    else:
        state = rules.unpack(snap[1])
        start_idx = snap[0] + 1  # snapshot 到 seq 为止（含），从下一条重放

    for i in range(start_idx, len(recorded)):
        d = recorded[i]
        state = rules.advance(rules.apply(state, d["action"]))
    return state


async def _speak_with_retry(
    actor: Actor,
    seat: int,
    prompt: str,
    turn_id: str,
    seq: int,
) -> tuple[str, dict[str, Any] | None]:
    """调 actor，带退避重试（沿用旧 AgentActor 的策略）。"""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await actor.speak(seat, prompt, turn_id, seq)
        except Exception as e:
            if attempt == _MAX_ATTEMPTS:
                raise
            delay = min(_BACKOFF_BASE * 2 ** (attempt - 1), _BACKOFF_CAP) + random.random()
            print(
                f"[retry] 座位{seat} turn={turn_id} 第 {attempt}/{_MAX_ATTEMPTS} 次失败，{delay:.1f}s 后重试：{e}",
                file=sys.stderr,
            )
            await _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def _sleep(delay: float) -> None:
    import asyncio

    await asyncio.sleep(delay)


# 旧 Runner 的 SIGINT 友好包装，loop 独立于 LangGraph 但仍需要它。
def run_async_main[T](coroutine) -> T:  # type: ignore[no-untyped-def]
    import asyncio
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
