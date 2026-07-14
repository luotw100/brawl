"""Rules：玩法插件协议。

唯一懂语义的地方。一个 rules 实现 = 一个玩法。core 持有一个 Rules 实例，从不读它
的内部 state（state 对 core 是 opaque token，由 rules 管理并序列化）。

契约方法分三组：

  循环驱动：
    initial_state / is_terminal / expected / advance
  语义解析（唯一懂玩法的地方）：
    view / parse
  状态推进（纯 fold）：
    apply
  持久化与定位：
    turn_id / pack / unpack

`expected` 返回"该谁、要什么"；core 把 `view` 发给该 seat 的 actor 拿回 raw；rules
`parse(raw)` 得到 (deliveries, action)；core 路由 deliveries、记一条 Sentence、过
`apply` + `advance` 推进 state。core 全程不碰 action 的字段。
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

from core.sr.types import Expectation, ParseResult

State_contra = TypeVar("State_contra", contravariant=True)  # noqa: F841 — 仅用于 Protocol 类型示意


@runtime_checkable
class Rules(Protocol):
    """玩法插件。state 由各实现自选类型，对 core 完全 opaque。"""

    def initial_state(self, setup: Any) -> Any:
        """从一份已校验的 setup 造初始状态。所有随机必须前置到这里（回放确定性不变量）。"""
        ...

    def is_terminal(self, state: Any) -> bool:
        """对局是否结束。"""
        ...

    def expected(self, state: Any) -> Expectation:
        """该谁说话、要遵守什么 schema、本回合 turn_id。终局时不应被调用。"""
        ...

    def view(self, seat: int, state: Any) -> str:
        """给某席位注入的最终 prompt 文本。含私密信息隔离（只取该席位有权看到的状态子集）。"""
        ...

    def parse(self, raw: str, state: Any, exp: Expectation) -> ParseResult:
        """解析 + 校验该席位产出的 raw。唯一懂语义的地方。

        校验不过抛 ValueError，core 会按重试策略重新对该 expectation 调 actor（呼应旧
        引擎 LangGraph 超步重试 + "清理失败轮消息"的语义）。
        """
        ...

    def apply(self, state: Any, action: str) -> Any:
        """纯 fold：吃一句解析后的 action，推进状态。计票/胜负查询都在此，无副作用。"""
        ...

    def advance(self, state: Any) -> Any:
        """阶段或回合推进。可在 apply 之后把状态机往前推一格到下一个 expected。"""
        ...

    def turn_id(self, state: Any) -> str:
        """回合定位，替代旧 decision_id(quest, attempt, phase, seat)。core 只当字符串用。"""
        ...

    def pack(self, state: Any) -> bytes:
        """state 序列化，resume 快照用。必须可被 unpack 还原且重放 apply 结果一致。"""
        ...

    def unpack(self, raw: bytes) -> Any:
        """pack 的逆。"""
        ...


# 旧名兼容：rules 即玩法。后续迁移期间 avalon/werewolf 各发一个 Rules 实现。
__all__ = ["Rules", "Expectation", "ParseResult"]
