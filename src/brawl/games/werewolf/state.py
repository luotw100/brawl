"""狼人杀状态与结构化决策。座位号一律 1 起，贯穿 state / 句子日志 / 录制。

铁律：隐藏身份、狼队互认名单、预言家验人结果、女巫药瓶与刀口——这些只进对应席位的
PlayerView 与该席位私有频道，以及给观众的事件流（channels 含 audience 的 Delivery）；
公开账本（broadcast）只记录桌面可见事实：谁发了言、谁出局且翻出什么牌、放逐票型。

NonEmptyStr：发言/内心戏去掉首尾空白后必须非空。这是字段内在校验，由 rules.parse 解析
JSON 时直接完成；抽出空发言会直接 parse 失败，由 loop 的重试兜住，绝不让空发言静默上桌。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

DeathReason = Literal["wolf_kill", "poison", "exile", "hunter_shot"]


class Side(StrEnum):
    GOOD = "good"  # 好人阵营：神职 + 平民
    WEREWOLF = "werewolf"  # 狼人阵营


class Role(StrEnum):
    WEREWOLF = "werewolf"  # 狼人，夜里同伴互认、共商刀口
    SEER = "seer"  # 预言家（神），每夜验一人阵营
    WITCH = "witch"  # 女巫（神），一瓶解药一瓶毒药
    HUNTER = "hunter"  # 猎人（神），被刀/被放逐时可开枪带走一人（被毒不可）
    IDIOT = "idiot"  # 白痴（神），被放逐时翻牌免死、此后失去投票权
    VILLAGER = "villager"  # 平民

    @property
    def side(self) -> Side:
        return Side.WEREWOLF if self is Role.WEREWOLF else Side.GOOD

    @property
    def team_kind(self) -> Literal["wolf", "god", "villager"]:
        if self is Role.WEREWOLF:
            return "wolf"
        if self is Role.VILLAGER:
            return "villager"
        return "god"


class Seat(BaseModel):
    """桌上一个参与者：几号位、什么角色、死活、能否投票、是否已翻牌。

    role 由边界（validation）摇定填好。alive/can_vote/revealed 随对局推进由 rules 的
    apply/advance 更新（出局、白痴翻牌失投票权等），不由 AI 决定。"""

    no: int
    persona_id: str
    strategy_id: str
    tone: str
    strategy: str
    model: str
    role: Role
    alive: bool = True
    can_vote: bool = True
    revealed: bool = False

    @property
    def side(self) -> Side:
        return self.role.side


class DeathRecord(BaseModel):
    seat_no: int
    role: Role
    reason: DeathReason
    day: int


class VoteRecord(BaseModel):
    """一轮放逐投票的公开结果：票型、最高票与实际放逐。"""

    day: int
    votes: dict[int, int]  # voter_no → target_no
    top_voted: int | None
    exiled: int | None
    tied: bool


class SeerCheck(BaseModel):
    """预言家某夜的查验结果。只进预言家自己的频道与观众事件流。"""

    target_no: int
    side: Side
    day: int


# ---------------------------------------------------------------- 玩家结构化产出（rules.parse 的 JSON schema）

class Action(BaseModel):
    """LLM 动作的统一严格边界；业务字段不得矫正类型，也不得夹带未声明字段。"""

    model_config = ConfigDict(strict=True, extra="forbid")


class WolfKill(Action):
    """狼人夜间提刀：刀口 + 一句狼队内夜聊（只在狼频道可见）+ 内心戏（只给观众）。"""

    phase: Literal["wolf_kill"]
    target_no: int
    speech: NonEmptyStr
    monologue: NonEmptyStr


class SeerAction(Action):
    phase: Literal["seer_check"]
    target_no: int
    monologue: NonEmptyStr


class WitchAction(Action):
    """女巫用药：是否用解药救今晚刀口 + 是否用毒（毒谁）。两瓶药状态由 rules 校验。"""

    phase: Literal["witch_action"]
    save: bool
    poison_target_no: int | None
    monologue: NonEmptyStr


class Statement(Action):
    """白天发言 / 遗言 / 赛后复盘的结构化产出。"""

    phase: Literal["last_words", "discuss", "banter"]
    speech: NonEmptyStr
    monologue: NonEmptyStr


class VoteAction(Action):
    phase: Literal["vote"]
    target_no: int
    monologue: NonEmptyStr


class HunterShot(Action):
    """猎人开枪：可带走一人，也可不开枪（target_no=None）。"""

    phase: Literal["hunter_shot"]
    target_no: int | None
    speech: NonEmptyStr
    monologue: NonEmptyStr
