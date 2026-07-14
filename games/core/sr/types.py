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

from typing import Literal

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

    - `turn_id`：rules.turn_id(state) 给（如 "quest3.attempt2.vote" / "day2.wolf_kill"）；core
      当字符串用，替代旧的 decision_id(quest, attempt, phase, seat)。回放层据此把
      sentence 与 llm_calls.jsonl 的审计行对齐。
    - `speaker`：席位号，或 "host"（主持人串场词，rules 纯函数模板生成）。
    - `raw`：该席位 opencode 的原始响应文本；给 replay / llm_calls join 存原文。
    - `deliveries`：路由后的多段话（一般 1 段；夜晚刀人可能 1 段给狼群 + 1 段 audience）。
    - `action`：rules.parse 解析出的结构化结果，序列化成字符串。**core 当 bytes 存**，从不读字段。
    - `seq`：core 赋的全局自增序号，日志定位用。
    """

    seq: int
    turn_id: str
    speaker: int | Literal["host"]
    raw: str = ""
    deliveries: list[Delivery] = Field(default_factory=list)
    action: str = ""


class Expectation(BaseModel):
    """该谁说话、要什么。

    `view` 是 rules.view(seat, state) 生成的最终 prompt 文本——core 原样发给该 seat 的
    opencode/session，不套任何通用模板（模板里写"你是 3 号狼人"就是玩法泄漏）。
    `schema_text` 是塞进 prompt 里告诉玩家"请按这个 JSON 形状回答"的约束文本，由 rules
    生成（因为合法形状是玩法专属的）。
    """

    seat: int | Literal["host"]
    turn_id: str
    view: str
    schema_text: str = ""


class ParseResult(BaseModel):
    """rules.parse 的产物：路由 + opaque action。唯一懂语义的解析点。"""

    deliveries: list[Delivery] = Field(default_factory=list)
    action: str = ""
