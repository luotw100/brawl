"""Rules：玩法插件协议。

唯一懂语义的地方。一个 rules 实现 = 一个玩法。runtime 持有一个 Rules 实例，从不读它
的内部 state（state 对 core 是 opaque token，由 rules 管理并序列化）。

契约方法分三组：

  循环驱动：
    initial_state / is_terminal / expected / advance
  语义解析（唯一懂玩法的地方）：
    view / parse
  状态推进（纯 fold）：
    apply
`expected` 返回"该谁、要什么"；core 把 `view` 发给该 seat 的 actor 拿回 raw；rules
`parse(raw)` 得到 (deliveries, action)；core 路由 deliveries、记一条 Sentence、过
`apply` + `advance` 推进 state。core 全程不碰 action 的字段。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from brawl.runtime.threads import SeatThread
from brawl.runtime.types import Expectation, HostEmit, ParseResult


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
        """该谁说话、本回合 turn_id。seats 长度 >1 即同时行动（core gather 并发收齐）。
        终局时不应被调用。"""
        ...

    def view(self, seat: int, state: Any, thread: SeatThread, *, first_speak: bool = False) -> Any:
        """给某席位拼一个 opaque 视图对象。

        `thread.inbox` 是该席位自上次发言以来累积的可见事件；
        `thread.messages` 是已提交的私有 human/assistant 历史。`first_speak=True`
        表示 actor 在本进程的新 session 里首次发言（冷续跑后也为 True），rules
        必须把固定设定和这份私有历史 seed 进 `.prompt`。

        view 是信息隔离的 fail-closed 边界：只接受当前 `expected(state).seats`
        中的 seat；非行动席位必须拒绝，而不是构造一个可能携带私密字段的视图。

        返回 view 的 `.prompt` 是实际发给 actor 的文本；`.turn_prompt` 是本轮增量
        human prompt，由 Sentence 持久化后写入 thread，不得包含首轮全量 wrapper。
        私密信息隔离（只取该席位有权看到的状态子集）全在这里。
        """
        ...

    def parse(self, seat: int, raw: str, state: Any) -> ParseResult:
        """解析 + 校验该席位产出的 raw。唯一懂语义的地方。

        模型产出不合规必须抛 brawl.runtime.parsing.JsonParseError，core 才会重新请求；
        其他异常视为规则实现错误并立即 fail-stop。
        """
        ...

    def apply(self, state: Any, action: str) -> Any:
        """纯 fold：吃一句解析后的 action，推进状态。计票/胜负查询都在此，无副作用。"""
        ...

    def advance(self, state: Any) -> Any:
        """阶段或回合推进。可在 apply 之后把状态机往前推一格到下一个 expected。"""
        ...

    def pending(self, state: Any) -> list[HostEmit]:
        """apply/advance 之后、下一次 expected 之前要落日志+路由的非玩家句子。

        host 串场词、观众事件（reveal / 计票公布 / 胜负）由本方法产出。loop 不对 host 调
        actor（host 是 rules 纯函数模板），拿到本列表后逐条记进 Sentence 日志、按 channels
        路由、但不 apply（这些句子的语义已由 apply/advance 在 state 里反映）。
        无则返回 []。终局前的 reveal 等也走这里。"""
        ...

# rules 即玩法：每个玩法提供一个 Rules 实现，core 持有一个实例驱动。
__all__ = ["Rules", "Expectation", "HostEmit", "ParseResult"]
