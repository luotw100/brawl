"""最小参考 Rules + Actor，验证 loop/journal/resume 三件套跑得通。

DummyRules：3 个席位轮流广播说一句，3 句后终局。覆盖 broadcast 拓扑 + pack/unpack
+ 句子日志 + resume 全流程。state 完全 opaque（一个 dict），core 从不读它。
"""

from __future__ import annotations

from typing import Any

from core.sr.rules import Rules as RulesProtocol  # noqa: F401 — 仅供类型提示
from core.sr.types import Delivery, Expectation, HostEmit, ParseResult

SEATS = [1, 2, 3]


class DummyRules:
    """3 席位 × 每人广播一句，3 句后终局。"""

    def initial_state(self, setup: Any) -> dict:
        # 所有随机前置到此处（本例无随机）。setup 形状由玩法自定，core 不看。
        return {"step": 0, "said": []}

    def is_terminal(self, state: dict) -> bool:
        return state["step"] >= len(SEATS)

    def expected(self, state: dict) -> Expectation:
        seat = SEATS[state["step"]]
        return Expectation(
            seat=seat, turn_id=f"dummy.{state['step']}.say", view=f"你是 {seat} 号，请说一句话。", schema_text=""
        )

    def view(self, seat: int, state: dict) -> str:
        return f"你是 {seat} 号，请说一句话。"  # 与 expected 内一致；loop 主路径用 expected.view

    def parse(self, raw: str, state: dict, exp: Expectation) -> ParseResult:
        # 玩家发言广播给全员（broadcast 含自己）。唯一懂语义的地方。
        return ParseResult(deliveries=[Delivery(text=raw, channels=["broadcast"])], action=raw)

    def apply(self, state: dict, action: str) -> dict:
        # 纯 fold：吃一句话（记录所说），不动 step。advance 才推进阶段。
        return {**state, "said": [*state["said"], action]}

    def advance(self, state: dict) -> dict:
        return {**state, "step": state["step"] + 1}

    def pending(self, state: dict) -> list[HostEmit]:
        """演示 host 串场：终局（step==3）时向观众广播一句收尾。每一步 pending 都是
        纯查询：只在 is_terminal 后返一句，其余返回 []——故 loop 终局 flush 不会重复。"""
        if state["step"] < len(SEATS):
            return []
        return [
            HostEmit(
                turn_id="dummy.host.closing",
                deliveries=[Delivery(text="主持人：本局结束。", channels=["broadcast", "audience"])],
                action='{"kind":"closing"}',
            )
        ]

    def turn_id(self, state: dict) -> str:
        return f"dummy.{state['step']}"

    def pack(self, state: dict) -> bytes:
        import json

        return json.dumps(state, ensure_ascii=False).encode("utf-8")

    def unpack(self, raw: bytes) -> dict:
        import json

        return json.loads(raw.decode("utf-8"))


class DummyActor:
    """回声 actor：按席位返回固定串，零网络。每一次 speak 都入账供断言。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str, int]] = []

    async def speak(self, seat: int, prompt: str, turn_id: str, seq: int) -> tuple[str, dict[str, Any] | None]:
        self.calls.append((seat, prompt, turn_id, seq))
        raw = f"{seat}号说：大家好"
        audit = {"call_id": turn_id, "seat_no": seat, "seq": seq, "ok": True}
        return raw, audit

    async def close(self) -> None: ...
