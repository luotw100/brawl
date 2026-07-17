"""驱动循环：整个引擎。替代 LangGraph 的 astream + 节点编排。

语义全在 rules，管道全在 loop：

    state = rules.initial_state(setup)
    while not rules.is_terminal(state):
        exp = rules.expected(state)                       # 该谁、turn_id；seats 长度>1 即同时
        raws = await gather(*[_act_with_retry(rules, actors[s], s, view(s), state, exp.turn_id)
                              for s in exp.seats])         # 并发收齐；speak+parse 同单元重试
        for s, raw, pr in raws:                           # 按座位序确定性落日志/路由/apply
            sent = Sentence(seq, exp.turn_id, s, raw, pr.deliveries, pr.action)
            journal.append_sentence(sent)
            inbox[s].clear(); deliver(pr.deliveries, threads)
            state = rules.apply(state, pr.action)
        flush(rules.pending(state))                        # 串场词（开票/reveal/收场）
        state = rules.advance(state); flush(rules.pending(state))

句子日志即 checkpoint：resume 时从 snapshot 重放 sentences，回到崩溃前状态。
没有两路流对齐，没有 sidecar/watermark，没有 torn 超步。
"""

from __future__ import annotations

import asyncio
import random
import sys
from typing import Any, Protocol

from core.sr.journal import Journal
from core.sr.rules import Rules
from core.sr.types import Delivery, Renderer, Sentence
from core.threads import SeatThread


class Actor(Protocol):
    """一个席位的说话者契约。core 用它替代旧的 ModelBackend + AgentActor 两层。

    rules.view 产出的 opaque 视图对象原样进来；actor 把它的 `.prompt` 发给该 seat 的
    opencode/session（或读它的结构化字段做本地模拟），拿回 (raw, audit)。audit 是可选
    调试元数据（OpenCodeActor 可带 usage/latency，RandomActor 可带随机标记），core 不
    强制落盘；本地审计已移除，审计源以 opencode server 自身为准。
    """

    async def speak(self, seat: int, view: Any, turn_id: str) -> tuple[str, dict[str, Any] | None]: ...

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


_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0


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
) -> Any:
    """跑一整局到终局，返回终局 state。

    `snapshot_every > 0` 时每 N 条 sentence 存一次 state 快照加速 resume；0 = 不存（靠重放）。
    若传入 `state`，则从该状态继续（用于 resume 后恢复）。
    """
    if state is None:
        state = rules.initial_state(setup)
    seq = -1
    spoken: set[int] = set()  # 本进程内已发言过的席位——rules.view 据此决定首句是否 seed 全量上下文
    # 已在日志里的 host 串场 turn_id（续跑时从日志重建）：同一串场词绝不重复落盘。
    # host emit 的 turn_id 按玩法约定唯一标识一次播报实例（含天数/任务/尝试号定位）。
    journaled_host_turns: set[str] = {
        d["turn_id"] for d in journal.load_sentences() if d.get("speaker") == "host"
    }

    def _flush_host() -> None:
        """落日志 + 路由当前 state 对应的 host/观众串场词（rules 纯函数模板产出，不调 actor）。

        这些句子的语义已在 apply/advance 里反映，不进 apply——core 只记 + 路由。
        rules.pending 必须是无状态纯查询：同一 state 反复调用返回同一批，否则会重发。
        续跑恢复后，pending 条件可能仍对已播报过的串场词成立（崩溃点在播报之后、下一
        玩家行动之前）——按 turn_id 去重，已落盘的串场词不再重记。
        """
        nonlocal seq
        for emit in rules.pending(state):
            if emit.turn_id in journaled_host_turns:
                continue
            seq = journal.next_seq()
            hsent = Sentence(seq=seq, turn_id=emit.turn_id, speaker="host", raw="",
                             deliveries=emit.deliveries, action=emit.action)
            journal.append_sentence(hsent)
            journaled_host_turns.add(emit.turn_id)
            if renderer is not None:
                renderer.render_sentence(hsent)
            deliver(emit.deliveries, threads)

    _flush_host()  # 开局串场（主持人开场、发牌揭示）：首句玩家行动之前先落

    while not rules.is_terminal(state):
        exp = rules.expected(state)
        seats = exp.seats
        for s in seats:
            if actors.get(s) is None:
                # host 串场词等非玩家 expectation：rules 应已用 pending 给出，无需 actor。
                # loop 不对 host 调 actor；到此分支意味着玩法把 host 放进 expected 却没在 rules 内消化。
                raise RuntimeError(f"no actor for seat={s!r}; host sentences must not go through actors")

        # 同时亮牌/扣牌：并发收齐所有座位的 raw（单席位即一次调用），每人 prompt 互不依赖。
        # speak+parse 作为一个原子单元重试：parse 不过（ValueError）等价于这次调用失败，
        # 重新调 actor 拿一份新产出（opencode 后端会先清理失败轮消息）。解析只读本轮回合
        # 开始时的 state，并发座位互不影响；落日志/apply 仍按座位序确定性进行。
        async def _ask(s: int, _state=state, _turn_id=exp.turn_id) -> tuple[int, str, dict[str, Any] | None, Any]:
            v = rules.view(s, _state, threads[s].inbox, first_speak=s not in spoken)
            raw, audit, pr = await _act_with_retry(rules, actors[s], s, v, _state, _turn_id)
            return s, raw, audit, pr

        gathered = await asyncio.gather(*(_ask(s) for s in seats))

        # 按座位序落日志 + 路由 + apply（确定性顺序，与并发无关）。
        for s, raw, _audit, pr in gathered:
            seq = journal.next_seq()
            sent = Sentence(seq=seq, turn_id=exp.turn_id, speaker=s, raw=raw,
                            deliveries=pr.deliveries, action=pr.action)
            journal.append_sentence(sent)
            if renderer is not None:
                renderer.render_sentence(sent)
            threads[s].inbox.clear()  # 该席位已看到它的 prompt（等价旧 commit 清 inbox）
            spoken.add(s)
            deliver(pr.deliveries, threads)
            state = rules.apply(state, pr.action)

        _flush_host()  # 本轮玩家行动后的串场（开票公布、阶段提示、终局 reveal 等）
        state = rules.advance(state)
        _flush_host()  # advance 后的串场（阶段交接、终局收场）

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

    # 与 run 同构的重放：apply 逐句、advance 按 expectation（turn_id 分组）一次。
    # 同时行动阶段（组队表决/放逐投票）一个 turn_id 对应多条玩家句子；若逐句 advance
    # 会用不完整账本结算（多数决/票型全错）——必须整组 apply 完再 advance。
    # 同 turn_id 的玩家句子在日志里必然连续（host 串场只在整组之后 flush），按此分组。
    group: list[str] = []
    current_turn: str | None = None

    def _flush_group() -> None:
        nonlocal state
        for action in group:
            state = rules.apply(state, action)
        state = rules.advance(state)

    for i in range(start_idx, len(recorded)):
        d = recorded[i]
        if d["speaker"] == "host":
            continue  # host 串场词不参与状态推进（语义已在玩家句子的 apply/advance 里反映）
        turn = d["turn_id"]
        if current_turn is not None and turn != current_turn:
            _flush_group()
            group = []
        current_turn = turn
        group.append(d["action"])
    if group:
        _flush_group()
    return state


async def _act_with_retry(
    rules: Rules,
    actor: Actor,
    seat: int,
    view: Any,
    state: Any,
    turn_id: str,
) -> tuple[str, dict[str, Any] | None, Any]:
    """调 actor + rules.parse，作为一个单元带退避重试（沿用旧 AgentActor 的策略）。

    actor 调用失败（网络/超时/agent error）与 parse 校验不过（ValueError：坏 JSON、
    非法目标）同等对待——都是"这一轮没拿到合法产出"，重新调 actor 拿新的。重试上限
    后抛出，中断本局（fail-fast 不兜底：绝不把非法产出静默写进日志）。
    """
    for retry_no in range(1, _MAX_RETRIES + 1):
        try:
            raw, audit = await actor.speak(seat, view, turn_id)
            pr = rules.parse(seat, raw, state)
            return raw, audit, pr
        except Exception as e:
            if retry_no == _MAX_RETRIES:
                raise
            delay = min(_BACKOFF_BASE * 2 ** (retry_no - 1), _BACKOFF_CAP) + random.random()
            print(
                f"[retry] 座位{seat} turn={turn_id} 第 {retry_no}/{_MAX_RETRIES} 次失败，{delay:.1f}s 后重试：{e}",
                file=sys.stderr,
            )
            await _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def _sleep(delay: float) -> None:
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
