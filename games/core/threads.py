"""每席位私有线程。路由与提交只由 Sentence Runtime 负责。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ThreadMessage(BaseModel):
    """席位线程里的一条消息：human=裁判灌入，assistant=该席位原文。"""

    role: Literal["human", "assistant"]
    content: str


class SeatThread(BaseModel):
    """一个席位的私有对话线程。"""

    inbox: list[str] = Field(default_factory=list)
    messages: list[ThreadMessage] = Field(default_factory=list)
