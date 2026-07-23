"""句子运行时的游戏盲类型。

这是 core 见到的全部"形状"：一段话 + 路由 + 一个 opaque action。玩法语义
（这是投票还是刀人、payload 长啥样、谁该听）全在 rules，core 当字节流存。

- `Delivery`：别人实际听到的一段自然语言 + 它要进哪些 inbox/broadcast/audience。
- `Sentence`：一次"谁说了句什么"的完整记录；多条 sentence 共享
  `commit_id` 时属于同一个原子 turn。
- `Expectation`：该谁说话、要遵守什么 JSON schema、这一回合的 turn_id。
- `ParseResult`：rules.parse 的产物——路由（deliveries）+ opaque action。

core 搜不到任何玩法名词。`action` 是字符串：序列化成什么由 rules 决定，core 只
`state = rules.apply(state, sent.action)`，从不读它。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

# bool 不是 seat；字符串数字也不能越过边界被 Pydantic 静默转换。
Seat = Annotated[int, Field(strict=True, gt=0)]

# 路由目标。int → 写入该 seat 的 inbox；"broadcast" → 全员 inbox；"audience" → 观众上帝视角流。
Channel = Seat | Literal["broadcast", "audience"]


class _StrictModel(BaseModel):
    """运行时边界模型：拒绝隐式转换与未知字段。"""

    model_config = ConfigDict(strict=True, extra="forbid")


class Delivery(_StrictModel):
    """一段话要递给谁。

    `text` 是别的席位（它们是 LLM）inbox 里会看到的内容。`spoken=False` 表示文字仍按
    channels 进入账本/inbox，但不是一句应由 TTS 念出的自然语言。
    """

    text: str
    channels: list[Channel] = Field(min_length=1)
    spoken: bool = True

    @field_validator("channels")
    @classmethod
    def _channels_are_unique(cls, channels: list[Channel]) -> list[Channel]:
        if len(channels) != len(set(channels)):
            raise ValueError("delivery channels must be unique")
        return channels


class Sentence(_StrictModel):
    """一次发言/动作的完整记录。

    - `turn_id`：rules.turn_id(state) 给的回合定位串（玩法自定义格式，core 只当字符串用）。
      回放层据此把 sentence 与对应 LLM 调用对齐（审计现在由 opencode server 承担）。
    - `speaker`：席位号，或 "host"（主持人串场词，rules 纯函数模板生成）。
    - `raw`：该 seat actor 的原始响应文本；给回放层存档。
    - `deliveries`：路由后的多段话（一般 1 段；私密动作可能 1 段给子群 + 1 段 audience）。
    - `action`：rules.parse 解析出的结构化结果，序列化成字符串。**core 当 bytes 存**，从不读字段。
    - `seq`：core 赋的全局自增序号，日志定位用。
    - `commit_id`：journal 赋的 turn 事务序号。同一批玩家句子及其 host
      串场共享一个 id；resume 只按这个物理边界重放，不猜 turn_id。
    - `prompt`：本轮记入席位私有线程的增量 human prompt。首次调用发给
      actor 的全量设定 wrapper 不落这里，避免多次 resume 后递归膨胀。
    """

    seq: int
    turn_id: str = Field(min_length=1)
    speaker: Seat | Literal["host"]
    commit_id: int = -1
    raw: str = ""
    prompt: str = ""
    deliveries: list[Delivery] = Field(default_factory=list)
    action: str = ""


class Renderer(Protocol):
    """框架级渲染器：loop 每生成一条 Sentence 就回调一次，让终端/舞台实时显示。

    实现者可以是 ConsoleRenderer（终端面板）、TTS 驱动、OBS overlay 等。
    玩法代码不应直接依赖具体实现；loop 只通过此协议调用。
    """

    def set_meta(self, meta: dict[str, Any] | None) -> None: ...
    def render_sentence(self, sentence: Sentence) -> None: ...


class Expectation(_StrictModel):
    """该谁说话、这一回合的 turn_id。

    `seats` 是本轮要发言的席位（≥1）。长度 >1 即"同时亮牌/扣牌"——core 用 asyncio.gather
    真并发收齐所有人的 raw，再按座位序落日志、解析、apply（贴合规则里表决/出牌的 simultaneity，
    每人各自的 prompt 互不依赖）。单席位就是普通的轮流发言。

    每个 seat 的最终 prompt 文本由 rules.view(seat, state, thread, first_speak=...) 给出——
    core 不套任何通用模板（模板里写"你是 3 号狼人"就是玩法泄漏）。core 把该 view 对象原样
    递给该 seat 的 actor，不读它的字段。
    """

    seats: list[Seat] = Field(min_length=1)
    turn_id: str = Field(min_length=1)

    @field_validator("seats")
    @classmethod
    def _seats_are_unique(cls, seats: list[int]) -> list[int]:
        if len(seats) != len(set(seats)):
            raise ValueError("expectation seats must be unique")
        return seats


class ParseResult(_StrictModel):
    """rules.parse 的产物：路由 + opaque action。唯一懂语义的解析点。"""

    deliveries: list[Delivery] = Field(default_factory=list)
    action: str = ""


class HostEmit(_StrictModel):
    """host/观众串场词：apply/advance 之后、下一次 expected 之前要落日志的非玩家句子。

    host 是 rules 纯函数模板产出（不调 actor）。speaker 固定 "host"；turn_id/路由/action 由
    rules 在此处给定。`action` 是给观众/stage 读的结构化 payload（如 reveal 的角色揭示），
    `deliveries` 的 text 是面向席位的自然语言话。两者同一条总线服务两端。
    """

    turn_id: str = Field(min_length=1)
    deliveries: list[Delivery] = Field(default_factory=list)
    action: str = ""
