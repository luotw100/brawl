"""游戏状态与共享数据结构。座位号一律 1 起且贯穿全程（state/事件/录制不混 0 基下标）。

铁律：Seat 里的角色(role)/夜晚情报(intel)、以及 Player 产出的内心独白(monologue)，
绝不进入其他席位可见的任何通道。阿瓦隆里每人**知道自己的身份**（这是规则），但别人的
身份与谁出了破坏票永远是秘密——各席位线程（SeatThread）里灌的公开事件只有发言、
组队投票的明牌结果、和每个任务的破坏票**张数**。

每个席位一条 append-only 消息线程：assistant 是该席位自己的自由文本原文（含内心戏，
私密、只在自己线程里），human 是裁判灌入的公开事件与提问。线程只增不改——这是
prompt 缓存命中的根基，也让模型能看到自己之前的想法。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# 公开发言与内心戏的字段类型：去掉首尾空白后必须非空。这是"字段内在/形状校验"——声明式收在
# 模型里，AgentPlayer 解析 JSON 时直接完成；该有发言却空了 / 该有内心戏却空了会直接 parse 失败
# （抛 JsonParseError），由 AgentPlayer._act 的退避重试兜住，绝不让空发言静默上桌。
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Side(StrEnum):
    GOOD = "good"  # 蓝方：亚瑟的忠臣
    EVIL = "evil"  # 红方：莫德雷德的爪牙


class Role(StrEnum):
    MERLIN = "merlin"  # 蓝方，夜里看到红方（莫德雷德除外）
    PERCIVAL = "percival"  # 蓝方，夜里看到梅林与莫甘娜（分不清谁是谁）
    SERVANT = "servant"  # 蓝方普通忠臣
    ASSASSIN = "assassin"  # 红方，终局可刺杀梅林翻盘
    MORGANA = "morgana"  # 红方，在派西维尔眼里与梅林难辨
    MORDRED = "mordred"  # 红方，梅林看不到他
    OBERON = "oberon"  # 红方，与同伙互不相认
    MINION = "minion"  # 红方普通爪牙

    @property
    def side(self) -> Side:
        return Side.EVIL if self in (Role.ASSASSIN, Role.MORGANA, Role.MORDRED, Role.OBERON, Role.MINION) else Side.GOOD


class Seat(BaseModel):
    """桌上一个参与者的数据快照：几号位、什么角色、夜晚看到了谁。阿瓦隆没人出局。
    persona_id（腔调）与 strategy_id（打法）均已接线，分别进 prompt。"""

    no: int  # 座位号，1..N
    persona_id: str  # 人设 id；入图前由边界(validation→run._build_seats)填好
    strategy_id: str  # 流派 id；同上由边界填好（无中性默认，必由 setup 指定）
    model: str | None = None  # 逻辑模型名（llm 后端用）；random 后端为 None 也无妨
    role: Role = Role.SERVANT  # 入图前由边界(validation→run._build_seats)摇定填好；默认仅为占位
    # 夜晚情报（座位号列表，不含自己），含义随角色——可见性规则全在 rules.night_intel。
    intel: list[int] = Field(default_factory=list)


class ProposalRecord(BaseModel):
    """一次组队提案的公开结果（含被否决的）：谁提的、提了谁、各人投了什么、若上场则
    破坏票几张。这些在桌面上全是明牌；谁出的破坏票不在此（那是秘密，只进 god-view 事件）。"""

    quest: int
    attempt: int
    leader_no: int
    team: list[int]
    votes: dict[int, bool]  # seat_no → 赞成?（开牌后公开）
    approved: bool
    fails: int | None = None  # 仅 approved 后有值：破坏票张数
    result: Literal["success", "fail"] | None = None  # 仅 approved 后有值


class Action(BaseModel):
    """LLM 动作的统一严格边界；业务字段不得矫正类型，也不得夹带未声明字段。"""

    model_config = ConfigDict(strict=True, extra="forbid")


class Proposal(Action):
    """组队阶段队长的结构化产出（AgentPlayer 直接解析后端返回的 JSON）。"""

    phase: Literal["propose"]
    team: list[int]  # 公开：带谁上任务（座位号，人数须等于本任务队伍人数）
    speech: NonEmptyStr  # 公开：组队宣言
    monologue: NonEmptyStr  # 仅观众可见：内心独白


class Statement(Action):
    """讨论轮的结构化产出。"""

    phase: Literal["discuss", "lady_announce", "banter"]
    speech: NonEmptyStr  # 公开：一句桌面发言
    monologue: NonEmptyStr  # 仅观众可见：内心独白


class TeamVote(Action):
    """组队表决的结构化产出。同时亮票：表态本身开牌后公开，独白只给观众。"""

    phase: Literal["team_vote"]
    approve: bool
    monologue: NonEmptyStr


class QuestCard(Action):
    """任务出牌的结构化产出。蓝方只能出成功（违者抛错）；谁出了什么永远保密。"""

    phase: Literal["quest"]
    success: bool
    monologue: NonEmptyStr


class Assassination(Action):
    """刺杀阶段刺客的结构化产出。"""

    phase: Literal["assassinate"]
    target_no: int  # 公开：刺谁
    speech: NonEmptyStr  # 公开：指认宣言
    monologue: NonEmptyStr


class LadyChoice(Action):
    """湖中仙女持有者的查验选择。选谁是公开的；查验到的真实阵营只有持有者知道。"""

    phase: Literal["lady_choose"]
    target_no: int
    monologue: NonEmptyStr

