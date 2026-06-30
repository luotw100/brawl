"""阿瓦隆的舞台协议：指令形状 + Scene 字段 + 配音名单。全部子类化框架信封（server.protocol）。

这些是"上帝视角的阿瓦隆"：发牌亮底、组队提案、表决、任务开牌、仙女查验、刺杀、终局翻底牌。
框架引擎只认基类，从不 import 这里——只有 avalon 的 plugin/adapter 与前端 avalon 渲染器认它。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from server.protocol import BaseScene, Directive, FeedItem, TimelineEntry

# ---------------------------------------------------------------- 舞台指令


class SeatInfo(BaseModel):
    no: int  # 座位号，1 起
    name: str  # 人设显示名（cast.yaml 解析，观众认人靠它）
    persona: str
    role: str  # god-view：观众全程看得到底牌
    side: Literal["good", "evil"]
    model: str
    avatar_url: str | None = None  # 厂商 logo（录制时烧进 replay/avatars/）；缺=该座位无头像，前端回退 hash 色
    model_display: str | None = None  # 对外展示的模型名（meta.model_labels 查得）；缺=不显，前端不渲染那行


class GameOpen(Directive):
    """开局横幅：人数与阵营配比（不带 replay 目录名——那是导播台的事）。
    seats 让头像开局即就座（身份面朝下，发牌时翻开）；老 replay 的 game_start 无 seats → 空列表，
    退回"deal 才出头像"的旧行为，不破坏旧录像。"""

    kind: Literal["game_open"] = "game_open"
    players: int
    good: int
    evil: int
    seats: list[SeatInfo] = Field(default_factory=list)


class Deal(Directive):
    """发牌亮底：座位表 + 首任队长 + 仙女初持者（god-view）。"""

    kind: Literal["deal"] = "deal"
    seats: list[SeatInfo]
    leader_no: int
    lady_holder: int | None


class Speech(Directive):
    """一句台词：发言气泡 + 内心独白（独白是给观众的乐子）。"""

    kind: Literal["speech"] = "speech"
    phase: Literal["propose", "discuss", "lady", "banter"]
    seat_no: int
    text: str
    monologue: str
    quest: int | None = None
    attempt: int | None = None
    team: list[int] | None = None  # 仅 propose：本次提的队
    lady_target: int | None = None  # 仅 lady：被查验者
    lady_truth: str | None = None  # 仅 lady：真实阵营（god-view 旁注）


class SeatVote(BaseModel):
    seat_no: int
    approve: bool
    monologue: str


class VoteReveal(Directive):
    """全员同时亮票。"""

    kind: Literal["vote_reveal"] = "vote_reveal"
    quest: int
    attempt: int
    votes: list[SeatVote]
    approved: bool
    rejections: int  # 累计连续否决数（5 连否决红胜）


class QuestCard(BaseModel):
    seat_no: int
    success: bool
    monologue: str  # 出牌瞬间的内心戏（god-view，给观众的乐子）


class QuestReveal(Directive):
    """任务开牌：成败 + 破坏票张数 + god-view 谁出的牌。"""

    kind: Literal["quest_reveal"] = "quest_reveal"
    quest: int
    team: list[int]
    cards: list[QuestCard]
    fails: int
    fails_required: int
    result: Literal["success", "fail"]


class Assassination(Directive):
    kind: Literal["assassination"] = "assassination"
    assassin_no: int
    target_no: int
    hit: bool
    text: str
    monologue: str


class GameOver(Directive):
    kind: Literal["game_over"] = "game_over"
    winner: Literal["good", "evil"]
    win_reason: str
    seats: list[SeatInfo]


# ---------------------------------------------------------------- 前情回顾（timeline）


class ProposalLog(TimelineEntry):
    """一次组队提案的里程碑：谁提了谁、表决通过/被否、谁赞成谁反对。"""

    kind: Literal["proposal"] = "proposal"
    quest: int
    attempt: int
    leader_no: int
    team: list[int]
    approved: bool
    approves: list[int]  # 投赞成的座位
    rejects: list[int]  # 投反对的座位


class QuestLog(TimelineEntry):
    """一次任务的里程碑：哪支队、成败、破坏票张数（god-view：谁出的破坏票）。"""

    kind: Literal["quest"] = "quest"
    quest: int
    team: list[int]
    result: Literal["success", "fail"]
    fails: int
    fail_seats: list[int]  # god-view：出了破坏票的座位（观众看得到）


# ---------------------------------------------------------------- 舞台状态


class AvalonScene(BaseScene):
    """阿瓦隆舞台快照：在框架 BaseScene（game/feed）之上叠座位、任务进度等玩法态。"""

    game: str = "avalon"
    seats: list[SeatInfo] = Field(default_factory=list)
    dealt: bool = False  # 身份是否已发牌翻开（game_start 置 False=面朝下，deal 置 True=亮身份）
    leader_no: int | None = None
    lady_holder: int | None = None
    quest_no: int = 1
    attempt: int = 1
    missions: list[str | None] = Field(default_factory=lambda: [None] * 5)  # "success"|"fail"|None
    rejections: int = 0
    team: list[int] = Field(default_factory=list)
    speaking: int | None = None  # 正在发言的座位（高亮）
    winner: str | None = None
    win_reason: str | None = None
    timeline: list[TimelineEntry] = Field(default_factory=list)  # 整局里程碑（提队/任务），前情回顾面板据此回放全程


# 每条公开发言事件的 (call_id 字段, 文本字段)。与 games/avalon/tts.py 的 _utterances 同一套名单：
# 那边合成谁、进库就核对谁，多一个少一个都会让"配音齐了没"判错。game_end 不配音故不在内。
VOICED = {
    "host": ("call_id", "text"),
    "propose": ("call_id", "speech"),
    "discuss": ("call_id", "text"),
    "lady": ("announce_call_id", "speech"),
    "assassination": ("call_id", "speech"),
    "banter": ("call_id", "text"),
}


__all__ = [
    "Assassination", "AvalonScene", "Deal", "FeedItem", "GameOpen", "GameOver",
    "ProposalLog", "QuestCard", "QuestLog", "QuestReveal", "SeatInfo", "SeatVote",
    "Speech", "VoteReveal", "VOICED",
]
