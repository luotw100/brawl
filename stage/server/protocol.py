"""舞台协议：服务端 ↔ 页面的消息形状。

设计（v2，"电视信号"语义）：source of truth 只有服务端 engine 里的一份 Scene。
公开 WS 频道上只有**一种**消息——`StateMsg`，每一拍（以及任何状态变化时）推送
完整的当前舞台状态；客户端是纯渲染函数，零状态、零折叠逻辑，首次连接与第 N 拍
收到的是同一种东西，重连即恢复。

隐私边界：公开频道不出现 replay 目录名（排片是导播台的事）与队列；这些只走
带 token 的 /api/state。

时间对齐：FeedItem 带 `started_at`（服务端 epoch 秒）+ `duration`，StateMsg 带
服务端当前时刻 `ts`——客户端用 (ts - started_at) 推出"这句话已经演到第几秒"，
打字机从正确字数续接、音频 `currentTime` 对齐，刷新/中途加入都无感。
"""

from __future__ import annotations

import time
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 舞台指令


class SeatInfo(BaseModel):
    no: int  # 座位号，1 起
    name: str  # 人设显示名（cast.yaml 解析，观众认人靠它）
    persona: str
    role: str  # god-view：观众全程看得到底牌
    side: Literal["good", "evil"]
    model: str


class GameOpen(BaseModel):
    """开局横幅：人数与阵营配比（不带 replay 目录名——那是导播台的事）。
    seats 让头像开局即就座（身份面朝下，发牌时翻开）；老 replay 的 game_start 无 seats → 空列表，
    退回"deal 才出头像"的旧行为，不破坏旧录像。"""

    kind: Literal["game_open"] = "game_open"
    players: int
    good: int
    evil: int
    seats: list[SeatInfo] = Field(default_factory=list)


class Deal(BaseModel):
    """发牌亮底：座位表 + 首任队长 + 仙女初持者（god-view）。"""

    kind: Literal["deal"] = "deal"
    seats: list[SeatInfo]
    leader_no: int
    lady_holder: int | None


class Speech(BaseModel):
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
    audio_url: str | None = None


class SeatVote(BaseModel):
    seat_no: int
    approve: bool
    monologue: str


class VoteReveal(BaseModel):
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


class QuestReveal(BaseModel):
    """任务开牌：成败 + 破坏票张数 + god-view 谁出的牌。"""

    kind: Literal["quest_reveal"] = "quest_reveal"
    quest: int
    team: list[int]
    cards: list[QuestCard]
    fails: int
    fails_required: int
    result: Literal["success", "fail"]


class Assassination(BaseModel):
    kind: Literal["assassination"] = "assassination"
    assassin_no: int
    target_no: int
    hit: bool
    text: str
    monologue: str
    audio_url: str | None = None


class GameOver(BaseModel):
    kind: Literal["game_over"] = "game_over"
    winner: Literal["good", "evil"]
    win_reason: str
    seats: list[SeatInfo]


class HostSpeech(BaseModel):
    """主持人台词（games 层显式 Host 的 host 事件）：天然的过场与报幕。"""

    kind: Literal["host"] = "host"
    name: str  # 主持人显示名（meta.setup.host_cast → cast.yaml；老 replay 无 host 事件，不存在缺名问题）
    beat: str  # opening / deal / propose_intro / team / vote_result / quest_result / lady / reveal / closing
    text: str
    audio_url: str | None = None


class Intermission(BaseModel):
    """幕间页（engine 生成，非 replay 事件）。下局是什么不公开——报幕功能后续做。"""

    kind: Literal["intermission"] = "intermission"
    last_winner: str | None = None
    last_reason: str | None = None
    next_ready: bool = False


Directive = Annotated[
    Union[GameOpen, Deal, Speech, VoteReveal, QuestReveal, Assassination, GameOver, HostSpeech, Intermission],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------- 前情回顾（timeline）


class ProposalLog(BaseModel):
    """一次组队提案的里程碑：谁提了谁、表决通过/被否、谁赞成谁反对。"""

    kind: Literal["proposal"] = "proposal"
    quest: int
    attempt: int
    leader_no: int
    team: list[int]
    approved: bool
    approves: list[int]  # 投赞成的座位
    rejects: list[int]  # 投反对的座位


class QuestLog(BaseModel):
    """一次任务的里程碑：哪支队、成败、破坏票张数（god-view：谁出的破坏票）。"""

    kind: Literal["quest"] = "quest"
    quest: int
    team: list[int]
    result: Literal["success", "fail"]
    fails: int
    fail_seats: list[int]  # god-view：出了破坏票的座位（观众看得到）


TimelineEntry = Annotated[Union[ProposalLog, QuestLog], Field(discriminator="kind")]


# ---------------------------------------------------------------- overlay 画布


class OverlayItem(BaseModel):
    """悬浮在演出之上的一片内容（礼物轮播、广告、口播……都是它）。

    位置用设计画布百分比（0~100），客户端按固定设计尺寸缩放，构图与设备无关。
    """

    id: str = ""  # 空 = 由服务端分配
    kind: Literal["text", "image", "marquee"]
    content: str  # text/marquee 是文案，image 是 URL
    x: float = 50.0  # 锚点横坐标（百分比，50=居中）
    y: float = 90.0
    width: float | None = None  # 占画布宽的百分比；None = 内容自适应
    style: dict[str, str] = Field(default_factory=dict)  # 透传成内联 CSS（fontSize/color/background…）
    ttl_seconds: float | None = None  # None = 常驻直到手动撤下


# ---------------------------------------------------------------- 舞台状态


class FeedItem(BaseModel):
    """一拍已上演的 directive + 它的演出时间窗。"""

    seq: int
    directive: Directive
    started_at: float  # 服务端 epoch 秒
    duration: float


class Scene(BaseModel):
    """舞台快照（唯一的 source of truth，engine 持有并折叠）。"""

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
    feed: list[FeedItem] = Field(default_factory=list)  # 服务端硬上限 FEED_CAP；下发时按窗口旋钮裁
    timeline: list[TimelineEntry] = Field(default_factory=list)  # 整局里程碑（提队/任务），前情回顾面板据此回放全程


class StateMsg(BaseModel):
    """公开频道唯一的消息：当前时刻的完整舞台状态。"""

    type: Literal["state"] = "state"
    ts: float = Field(default_factory=time.time)  # 服务端当前时刻（客户端对钟用）
    phase: Literal["idle", "playing", "intermission"]
    paused: bool
    scene: Scene
    overlays: list[OverlayItem]
    prefetch: list[str] = Field(default_factory=list)  # 接下来几拍的音频 URL，页面提前预热免得起拍才拉、尾部被截
