"""core.runtime.actor 的单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.runtime.actor import OpenCodeActor, RandomActor
from core.runtime.messages import ChatMessage
from core.runtime.opencode_backend import ModelBackend


class FakeBackend(ModelBackend):
    """记录调用参数并返回固定文本。"""

    def __init__(self, text: str = "我同意。") -> None:
        self.calls: list[tuple[int, Any, list[ChatMessage]]] = []
        self._text = text

    async def call(self, phase: str, view: Any, messages: list[ChatMessage]) -> ChatMessage:
        self.calls.append((view.seat_no, view, messages))
        return ChatMessage(role="assistant", content=self._text, usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})

    def commit_turn(self, seat_no: int) -> None: ...

    async def rollback(self, seat_no: int) -> None: ...

    async def close(self) -> None: ...


@pytest.mark.asyncio
async def test_opencode_actor_passes_prompt():
    backend = FakeBackend()
    actor = OpenCodeActor(backend)
    view = SimpleNamespace(prompt="你是1号，请发言。", seat_no=42)

    raw = await actor.speak(seat=1, view=view, turn_id="avalon.1.speak")

    assert raw == "我同意。"
    assert backend.calls[-1][0] == 1  # 代理补上的 seat_no 是 speak 参数，不是 view 里的 seat_no
    assert len(backend.calls[-1][2]) == 1
    assert backend.calls[-1][2][0].content == "你是1号，请发言。"


@pytest.mark.asyncio
async def test_opencode_actor_infers_seat_no_when_view_lacks_it():
    backend = FakeBackend()
    actor = OpenCodeActor(backend)
    view = SimpleNamespace(prompt="请发言。")  # 没有 seat_no

    await actor.speak(seat=3, view=view, turn_id="x")
    assert backend.calls[-1][0] == 3


@pytest.mark.asyncio
async def test_random_actor_returns_fixed_answer():
    actor = RandomActor(lambda seat, view: "投赞成票")
    raw = await actor.speak(2, SimpleNamespace(prompt="x"), "vote")
    assert raw == "投赞成票"
