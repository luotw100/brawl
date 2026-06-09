"""出向事件流：节点经 LangGraph 的自定义流(StreamWriter)往外推 god-view 事件。

这是 games 唯一的对外出口。将来 stage(画面) / live(抖音互动) 订阅同一条流，引擎不改。
观众是上帝视角，故事件可携带全部信息（身份/词/独白）；对 AI 的信息屏蔽由 state.Utterance
不含这些字段来保证，与本流无关。
"""

from __future__ import annotations

from typing import Any

from langgraph.config import get_stream_writer


def emit(event_type: str, **fields: Any) -> None:
    """往自定义流推一个事件。runner 用 graph.astream(stream_mode="custom") 消费。"""
    writer = get_stream_writer()
    writer({"type": event_type, **fields})
