"""出向事件流：节点经 LangGraph 的自定义流(StreamWriter)往外推 god-view 事件。

这是 games 唯一的对外出口。将来 stage(画面) / live(抖音互动) 订阅同一条流，引擎不改。
观众是上帝视角，故事件可携带全部信息（角色/情报/独白/谁出了破坏票）；对 AI 的信息
屏蔽由 state.Utterance / ProposalRecord 不含这些字段来保证，与本流无关。
"""

from __future__ import annotations

from typing import Any

from langgraph.config import get_stream_writer


def decision_id(quest: int, attempt: int, phase: str, seat_no: int) -> str:
    """一次玩家决策的确定性标识——回放层据此把 events 与 llm_calls 严格对齐。

    纯函数，无状态：节点(发事件)与 Player(落 llm_call)各自用同一 (quest,attempt,phase,seat)
    算出同一个串，不必互相串数据。主调用与它的 `.extract` 抽取调用共享同一 id（base 已剥后缀），
    故回放端 `index[call_id]` 直接拿到 (主, extract) 这一对。座位号 1 起。"""
    base = phase.removesuffix(".extract")
    return f"{quest}.{attempt}.{base}.{seat_no}"


def emit(event_type: str, **fields: Any) -> None:
    """往自定义流推一个事件。runner 用 graph.astream(stream_mode="custom") 消费。"""
    writer = get_stream_writer()
    writer({"type": event_type, **fields})
