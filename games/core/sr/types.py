"""句子运行时的游戏盲类型。

这是 core 见到的全部"形状"：一段话 + 路由 + 一个 opaque action。玩法语义
（这是投票还是刀人、payload 长啥样、谁该听）全在 rules，core 当字节流存。

- `Delivery`：别人实际听到的一段自然语言 + 它要进哪些 inbox/broadcast/audience。
- `Sentence`：一次"谁说了句什么"的完整记录，落盘即一行，是 resume 的原子单位。
- `Expectation`：该谁说话、要遵守什么 JSON schema、这一回合的 turn_id。
- `ParseResult`：rules.parse 的产物——路由（deliveries）+ opaque action。

core 搜不到任何玩法名词。`action` 是字符串：序列化成什么由 rules 决定，core 只
`state = rules.apply(state, sent.action)`，从不读它。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

# 路由目标。int → 写入该 seat 的 inbox；"broadcast" → 全员 inbox；"audience" → 观众上帝视角流。
Channel = int | Literal["broadcast", "audience"]


class Delivery(BaseModel):
    """一段话要递给谁。

    `text` 是别的席位（它们是 LLM）inbox 里会看到的内容；audience/stage 则读 `action`
    里的结构化 payload（见 §8）。两者各取所需，同一条 Sentence 服务两端。
    """

    text: str
    channels: list[Channel] = Field(default_factory=list)


class Sentence(BaseModel):
    """一次发言/动作的完整记录。core 的 checkpoint 原子单位。

    - `turn_id`：rules.turn_id(state) 给的回合定位串（玩法自定义格式，core 只当字符串用）。
      回放层据此把 sentence 与对应 LLM 调用对齐（审计现在由 opencode server 承担）。
    - `speaker`：席位号，或 "host"（主持人串场词，rules 纯函数模板生成）。
    - `raw`：该 seat actor 的原始响应文本；给回放层存档。
    - `deliveries`：路由后的多段话（一般 1 段；私密动作可能 1 段给子群 + 1 段 audience）。
    - `action`：rules.parse 解析出的结构化结果，序列化成字符串。**core 当 bytes 存**，从不读字段。
    - `seq`：core 赋的全局自增序号，日志定位用。
    """

    seq: int
    turn_id: str
    speaker: int | Literal["host"]
    raw: str = ""
    deliveries: list[Delivery] = Field(default_factory=list)
    action: str = ""


class Renderer(Protocol):
    """框架级渲染器：loop 每生成一条 Sentence 就回调一次，让终端/舞台实时显示。

    实现者可以是 ConsoleRenderer（终端面板）、TTS 驱动、OBS overlay 等。
    玩法代码不应直接依赖具体实现；loop 只通过此协议调用。
    """

    def set_meta(self, meta: dict[str, Any] | None) -> None: ...
    def render_sentence(self, sentence: Sentence) -> None: ...


class Expectation(BaseModel):
    """该谁说话、这一回合的 turn_id。

    `seats` 是本轮要发言的席位（≥1）。长度 >1 即"同时亮牌/扣牌"——core 用 asyncio.gather
    真并发收齐所有人的 raw，再按座位序落日志、解析、apply（贴合规则里表决/出牌的 simultaneity，
    每人各自的 prompt 互不依赖）。单席位就是普通的轮流发言。

    每个 seat 的最终 prompt 文本由 rules.view(seat, state, inbox, first_speak=...) 给出——
    core 不套任何通用模板（模板里写"你是 3 号狼人"就是玩法泄漏）。core 把该 view 对象原样
    递给该 seat 的 actor，不读它的字段。
    """

    seats: list[int]
    turn_id: str


class ParseResult(BaseModel):
    """rules.parse 的产物：路由 + opaque action。唯一懂语义的解析点。"""

    deliveries: list[Delivery] = Field(default_factory=list)
    action: str = ""


class HostEmit(BaseModel):
    """host/观众串场词：apply/advance 之后、下一次 expected 之前要落日志的非玩家句子。

    host 是 rules 纯函数模板产出（不调 actor）。speaker 固定 "host"；turn_id/路由/action 由
    rules 在此处给定。`action` 是给观众/stage 读的结构化 payload（如 reveal 的角色揭示），
    `deliveries` 的 text 是面向席位的自然语言话。两者同一条总线服务两端（见 §8）。
    """

    turn_id: str
    deliveries: list[Delivery] = Field(default_factory=list)
    action: str = ""
