"""一次 agent 调用的消息类型。

项目早期用 langchain-core 的 HumanMessage/AIMessage 当消息容器——只用了 role+content
两个字段，却拖进整条 langchain 依赖链。这里 20 行自带，语义不变：human 是调用方渲染的
prompt，assistant 是后端返回的文本（可带 token usage 元数据）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """一条对话消息：role + content，assistant 可带 usage。"""

    role: Literal["system", "human", "assistant"]
    content: str
    usage_metadata: dict[str, Any] = Field(default_factory=dict)
