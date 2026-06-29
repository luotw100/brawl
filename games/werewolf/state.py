"""狼人杀状态与结构化决策。座位号一律 1 起，贯穿 state / 事件 / 录制。

铁律：隐藏身份、狼队互认名单、预言家验人结果、女巫药瓶与刀口——这些只进对应席位的
PlayerView 与该席位私有线程（SeatThread），以及给观众的 god-view 事件流；公开账本
（进全员 inbox 的广播行）只记录桌面可见事实：谁发了言、谁出局且翻出什么牌、放逐票型。

每席位一条 append-only 消息线程：assistant 是该席位自己的自由文本原文（含内心戏，私密），
human 是裁判灌入的公开事件与提问。线程只增不改——prompt 缓存命中与"模型看得到自己之前的
想法"都靠这条。

NonEmptyStr：发言/内心戏去掉首尾空白后必须非空。这是字段内在校验，声明式收在模型里随
extractor 抽取一起完成——抽出空发言会直接 parse 失败，由 LLMPlayer 退避重试兜住，绝不让
空发言静默上桌。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from core.threads import SeatThread
from werewolf.rules import Role, Side

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

DeathReason = Literal["wolf_kill", "poison", "exile", "hunter_shot"]


class Seat(BaseModel):
    """桌上一个参与者：几号位、什么角色、死活、能否投票、是否已翻牌。

    role 入图前由边界（validation→run._build_seats）摇定填好。alive/can_vote/revealed
    随对局推进由裁判节点更新（出局、白痴翻牌失投票权等），不由 AI 决定。"""

    no: int
    persona_id: str
    strategy_id: str
    model: str | None = None
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
    """一轮放逐投票的公开结果：谁投了谁、放逐了谁、是否平票。票型全是明牌。"""

    day: int
    votes: dict[int, int]  # voter_no → target_no
    exiled: int | None
    tied: bool


class SeerCheck(BaseModel):
    """预言家某夜的查验结果。只进预言家自己的线程与 god-view 事件流。"""

    target_no: int
    side: Side
    day: int


# ---------------------------------------------------------------- 玩家结构化产出（extractor 抽取）

class WolfKill(BaseModel):
    """狼人夜间提刀：刀口 + 一句狼队内夜聊（只在狼频道可见）+ 内心戏（只给观众）。"""

    target_no: int
    speech: NonEmptyStr
    monologue: NonEmptyStr


class SeerAction(BaseModel):
    target_no: int
    monologue: NonEmptyStr


class WitchAction(BaseModel):
    """女巫用药：是否用解药救今晚刀口 + 是否用毒（毒谁）。两瓶药状态由裁判校验。"""

    save: bool = False
    poison_target_no: int | None = None
    monologue: NonEmptyStr


class Statement(BaseModel):
    """白天发言 / 遗言 / 赛后复盘的结构化产出。"""

    speech: NonEmptyStr
    monologue: NonEmptyStr


class VoteAction(BaseModel):
    target_no: int
    monologue: NonEmptyStr


class HunterShot(BaseModel):
    """猎人开枪：可带走一人，也可不开枪（target_no=None）。"""

    target_no: int | None = None
    speech: NonEmptyStr
    monologue: NonEmptyStr


class GameState(BaseModel):
    """LangGraph 状态。threads 每节点整体覆盖；列表字段由节点显式拼新值覆盖；其余整体覆盖。

    phase 是昼夜机的位置，仅供 check 后路由判断走白天还是黑夜：
      opening      初始（开局到首夜之间）
      night        夜晚行动中（night_start 后）
      after_night  夜晚已结算（resolve_night 后）→ 路由去白天
      after_vote   放逐已结算（resolve_vote 后）→ 路由去下一夜（并令 day+1）
    """

    seats: list[Seat]
    board: str
    threads: dict[int, SeatThread] = Field(default_factory=dict)
    day: int = 1
    phase: Literal["opening", "night", "after_night", "after_vote"] = "opening"
    deaths: list[DeathRecord] = Field(default_factory=list)
    round_deaths: list[DeathRecord] = Field(default_factory=list)
    votes: list[VoteRecord] = Field(default_factory=list)
    seer_checks: list[SeerCheck] = Field(default_factory=list)
    wolf_target: int | None = None
    witch_saved: bool = False
    witch_poison_target: int | None = None
    witch_has_antidote: bool = True
    witch_has_poison: bool = True
    pending_hunter: int | None = None  # 待开枪的猎人座位（死于刀/放逐才有，死于毒为 None）
    winner: str | None = None  # "good" | "werewolf"
    win_reason: str | None = None
    finished: bool = False

    @model_validator(mode="after")
    def _init_threads(self) -> GameState:
        if not self.threads:
            self.threads = {s.no: SeatThread() for s in self.seats}
        return self

    def living(self) -> list[Seat]:
        return [s for s in self.seats if s.alive]

    def living_nos(self) -> list[int]:
        return [s.no for s in self.living()]

    def seat(self, no: int) -> Seat:
        return self.seats[no - 1]
