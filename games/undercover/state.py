"""游戏状态与共享数据结构。

铁律：Seat 里的身份(role)/词(word)、以及 Player 产出的内心独白(monologue)，
绝不进入 Utterance（AI 之间可见的公开记录）。这是全部戏剧张力与"独白只给观众"的保证。
"""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Role(StrEnum):
    CIVILIAN = "civilian"
    UNDERCOVER = "undercover"
    BLANK = "blank"


class Seat(BaseModel):
    """桌上一个参与者的数据快照：谁坐这、拿什么身份/词、由哪个模型驱动、是否存活。"""

    no: int
    persona_id: str
    strategy_id: str = "standard"  # 流派 id；入图前由边界(validation→run._build_seats)填好
    name: str
    model: str | None = None  # 逻辑模型名（llm 后端用）；random 后端为 None 也无妨
    role: Role = Role.CIVILIAN  # 入图前由边界(validation→run._build_seats)摇定填好；默认仅为占位
    word: str = ""  # 入图前由边界填好；白板为 ""
    alive: bool = True


class Utterance(BaseModel):
    """AI 之间可见的公开发言记录。只含公开信息——不含身份、词、独白。"""

    round: int
    seat_no: int
    name: str
    kind: Literal["describe", "vote"]
    text: str
    target_no: int | None = None  # kind==vote 时投给谁


class Describe(BaseModel):
    """描述轮的结构化产出。"""

    description: str  # 公开：一句话描述自己的词（不能说出词本身）
    monologue: str  # 仅观众可见：内心独白


class Vote(BaseModel):
    """投票轮的结构化产出。"""

    target_no: int  # 公开：投给哪个座位号
    reason: str  # 公开：一句理由
    monologue: str  # 仅观众可见：内心独白


class GameState(BaseModel):
    """LangGraph 状态。transcript 用 add reducer 累积；其余字段整体覆盖。"""

    round: int = 0
    seats: list[Seat]
    transcript: Annotated[list[Utterance], operator.add] = Field(default_factory=list)
    votes: dict[int, int] = Field(default_factory=dict)  # 本轮 seat_no -> target_no
    last_eliminated: int | None = None
    winner: str | None = None  # "civilians" | "undercover"
    finished: bool = False

    def alive_seats(self) -> list[Seat]:
        return [s for s in self.seats if s.alive]
