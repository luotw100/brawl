"""每席位私有线程与公开广播工具。"""

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


def copy_threads(threads: dict[int, SeatThread]) -> dict[int, SeatThread]:
    """取一份可变工作副本；节点在副本上追加，整体作为状态更新返回。"""

    return {no: t.model_copy(deep=True) for no, t in threads.items()}


def broadcast(threads: dict[int, SeatThread], line: str) -> None:
    """公开行进入全员 inbox。"""

    for t in threads.values():
        t.inbox.append(line)


def commit(threads: dict[int, SeatThread], no: int, prompt: str, raw: str) -> None:
    """某席位完成一次行动：清空 inbox，追加 human/assistant 消息。"""

    t = threads[no]
    t.inbox.clear()
    t.messages.append(ThreadMessage(role="human", content=prompt))
    t.messages.append(ThreadMessage(role="assistant", content=raw))

