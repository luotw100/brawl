"""Sentence Runtime 测试共用的最小 Rules 与 Actor 夹具。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from brawl.runtime.types import Delivery, Expectation, HostEmit, ParseResult

SEATS = [1, 2, 3]


class DummyRules:
    """3 席位每人广播一句，3 句后终局。"""

    def initial_state(self, setup: Any) -> dict:
        return {"step": 0, "said": []}

    def is_terminal(self, state: dict) -> bool:
        return state["step"] >= len(SEATS)

    def expected(self, state: dict) -> Expectation:
        seat = SEATS[state["step"]]
        return Expectation(seats=[seat], turn_id=f"dummy.{state['step']}.say")

    def view(
        self,
        seat: int,
        state: dict,
        thread: Any,
        *,
        first_speak: bool = False,
    ) -> Any:
        prompt = f"你是 {seat} 号，请说一句话。"
        return SimpleNamespace(prompt=prompt, turn_prompt=prompt)

    def parse(self, seat: int, raw: str, state: dict) -> ParseResult:
        return ParseResult(
            deliveries=[Delivery(text=raw, channels=["broadcast"], kind="speech")],
            action=raw,
        )

    def apply(self, state: dict, action: str) -> dict:
        return {**state, "said": [*state["said"], action]}

    def advance(self, state: dict) -> dict:
        return {**state, "step": state["step"] + 1}

    def pending(self, state: dict) -> list[HostEmit]:
        if state["step"] < len(SEATS):
            return []
        return [
            HostEmit(
                turn_id="dummy.host.closing",
                deliveries=[
                    Delivery(
                        text="主持人：本局结束。",
                        channels=["broadcast", "audience"],
                    )
                ],
                action='{"kind":"closing"}',
            )
        ]


class DummyActor:
    """按席位回声的零网络 Actor。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []

    async def speak(self, seat: int, view: Any, turn_id: str) -> str:
        self.calls.append((seat, view.prompt, turn_id))
        return "大家好"

    def commit_turn(self, seat: int) -> None: ...

    async def rollback(self, seat: int) -> None: ...
