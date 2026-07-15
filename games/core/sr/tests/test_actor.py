"""core.sr.actor 的单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from core.models import ModelSpec
from core.sr.actor import OpenCodeActor, RandomActor
from core.sr.opencode_backend import ModelBackend


class FakeBackend(ModelBackend):
    """记录调用参数并返回固定文本。"""

    def __init__(self, text: str = "我同意。") -> None:
        self.calls: list[tuple[int, Any, Any, list[BaseMessage]]] = []
        self._text = text

    async def call(self, phase: str, view: Any, spec: ModelSpec | None, messages: list[BaseMessage]) -> BaseMessage:
        self.calls.append((view.seat_no, view, spec, messages))
        return AIMessage(content=self._text, usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})


@pytest.mark.asyncio
async def test_opencode_actor_passes_prompt_and_records_audit():
    backend = FakeBackend()
    actor = OpenCodeActor(backend)
    spec = ModelSpec(name="gpt-4", opencode_model="openai/gpt-4")
    view = SimpleNamespace(prompt="你是1号，请发言。", spec=spec, seat_no=42)

    raw, audit = await actor.speak(seat=1, view=view, turn_id="avalon.1.speak")

    assert raw == "我同意。"
    assert backend.calls[-1][0] == 1  # 代理补上的 seat_no 是 speak 参数，不是 view 里的 seat_no
    assert len(backend.calls[-1][3]) == 1
    assert backend.calls[-1][3][0].content == "你是1号，请发言。"
    assert audit["seat_no"] == 1
    assert audit["call_id"] == "avalon.1.speak"
    assert audit["model"] == "openai/gpt-4"
    assert audit["ok"] is True
    assert audit["output"] == "我同意。"
    assert audit["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_opencode_actor_infers_seat_no_when_view_lacks_it():
    backend = FakeBackend()
    actor = OpenCodeActor(backend)
    view = SimpleNamespace(prompt="请发言。")  # 没有 seat_no

    await actor.speak(seat=3, view=view, turn_id="x")
    assert backend.calls[-1][0] == 3


@pytest.mark.asyncio
async def test_random_actor_returns_fixed_answer():
    actor = RandomActor("投赞成票")
    raw, audit = await actor.speak(2, SimpleNamespace(prompt="x"), "vote")
    assert raw == "投赞成票"
    assert audit["seat_no"] == 2
    assert audit["model"] == "random"
