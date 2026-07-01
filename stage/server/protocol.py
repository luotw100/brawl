"""舞台协议（框架信封）：服务端 ↔ 页面的消息形状里**与玩法无关**的那一层。

设计（v2，"电视信号"语义）：source of truth 只有服务端 engine 里的一份 Scene。
公开 WS 频道上只有**一种**消息——`StateMsg`，每一拍（以及任何状态变化时）推送
完整的当前舞台状态；客户端是纯渲染函数，零状态、零折叠逻辑，首次连接与第 N 拍
收到的是同一种东西，重连即恢复。

游戏无关边界：这个文件只放"信封"——指令基类、主持台词、幕间、旁白、overlay、
FeedItem、Scene 基类、StateMsg。**具体玩法的指令形状（阿瓦隆的发牌/表决/任务……）
与 Scene 字段在 games/<game>/protocol.py，子类化这里的基类。** 框架引擎只认基类，
不认任何具体指令——加玩法即并列一个 plugin，引擎零分叉。

序列化边界：`FeedItem.directive` 与 `StateMsg.scene` 声明为基类但用 `SerializeAsAny`
透传——引擎持有的是具体子类（如 AvalonScene / Speech），dump 时按子类的真实字段
全量序列化给页面；框架自身只序列化、从不反解这些 Scene/Directive，故无需跨玩法判别联合。

隐私边界：公开频道不出现 replay 目录名（排片是导播台的事）与队列；这些只走
带 token 的 /api/state。

时间对齐：FeedItem 带 `started_at`（服务端 epoch 秒）+ `duration`，StateMsg 带
服务端当前时刻 `ts`——客户端用 (ts - started_at) 推出"这句话已经演到第几秒"，
打字机从正确字数续接、音频 `currentTime` 对齐，刷新/中途加入都无感。
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field, SerializeAsAny

# ---------------------------------------------------------------- 指令基类（框架只认这层）


class Directive(BaseModel):
    """舞台指令基类：框架只依赖 `kind` 做计划编译与页面渲染分派。
    `audio_url` 是主语音载荷，PlayPlan 会把它投影成 tracks；直播/录制不直接重解它。
    具体玩法的指令子类化它、自带各自字段；框架引擎从不 match 具体子类。"""

    kind: str
    audio_url: str | None = None


class HostSpeech(Directive):
    """主持人台词（games 层显式 Host 的 host 事件）：天然的过场与报幕。各玩法共用一个主持人，
    故它是框架指令；`beat` 字符串的取值各玩法自定义（哪些 beat 算 lead-in 由 PlayPlan DSL 决定）。"""

    kind: Literal["host"] = "host"
    name: str  # 主持人显示名（meta.setup.host_name → cast.yaml；老 replay 无 host 事件，不存在缺名问题）
    beat: str  # 玩法自定义的报幕节点名（avalon：opening/deal/propose_intro/.../closing）
    text: str


class Intermission(Directive):
    """幕间页（engine 生成，非 replay 事件）。下局是什么不公开——报幕功能后续做。"""

    kind: Literal["intermission"] = "intermission"
    last_winner: str | None = None
    last_reason: str | None = None
    next_ready: bool = False


class GameResult(BaseModel):
    """一局终局的中性摘要：引擎据此填幕间页（谁赢、为何赢），不认各玩法 winner 的具体取值。"""

    winner: str
    win_reason: str


class NarrationCue(BaseModel):
    """挂在一拍视觉之下的主持旁白。at 是拍内偏移秒；duration 是旁白字幕/音频预算。"""

    name: str
    beat: str
    text: str
    audio_url: str | None = None
    at: float = 0.0
    duration: float = 0.0


class AudioTrack(BaseModel):
    """一拍内的确定性音轨计划。直播页和 stage-record 都只消费这张轨道表。

    start/duration 已经是拍内秒数，并已按 engine speed 压缩；播放侧把音频 playbackRate 同步设为
    speed。sfx 轨使用 /sfx/<name>.opus，replay 人声使用 /replays/<id>/tts/<file>。
    """

    kind: str  # main | narration | sfx
    audio_url: str | None = None
    start: float = 0.0
    duration: float = 0.0
    volume: float = 1.0
    text: str = ""
    name: str | None = None
    beat: str | None = None


# ---------------------------------------------------------------- 前情回顾（timeline）


class TimelineEntry(BaseModel):
    """前情回顾里程碑基类：框架只认 `kind`，具体条目（提队/任务/出局……）由各玩法子类化。"""

    kind: str


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
    """一拍已上演的 directive + 它的演出时间窗。directive 声明为基类、`SerializeAsAny` 透传具体子类。"""

    seq: int
    directive: SerializeAsAny[Directive]
    started_at: float  # 服务端 epoch 秒
    duration: float
    speed: float = 1.0  # 时间轴倍速：duration 已按它压缩；播放侧据此把音频 playbackRate 提速贴合
    content_at: float = 0.0  # 主内容拍内偏移：前置主持旁白先说完，玩家发言再开始
    narrations: list[NarrationCue] = Field(default_factory=list)
    tracks: list[AudioTrack] = Field(default_factory=list)


class BaseScene(BaseModel):
    """舞台快照基类（唯一的 source of truth，engine 持有并折叠）。

    框架只依赖 `game`（页面据此挑渲染器）与 `feed`（窗口裁剪 / 时间对齐）这两个字段；
    座位、任务进度等玩法状态在 games/<game>/protocol.py 的子类里。`SerializeAsAny` 保证
    引擎持有的具体子类按真实字段全量下发。"""

    game: str = ""
    feed: list[FeedItem] = Field(default_factory=list)  # 服务端硬上限 FEED_CAP；下发时按窗口旋钮裁


class StateMsg(BaseModel):
    """公开频道唯一的消息：当前时刻的完整舞台状态。"""

    type: Literal["state"] = "state"
    ts: float = Field(default_factory=time.time)  # 服务端当前时刻（客户端对钟用）
    phase: Literal["idle", "playing", "intermission"]
    scene: SerializeAsAny[BaseScene]
    overlays: list[OverlayItem]
    prefetch: list[str] = Field(default_factory=list)  # 接下来几拍的音频 URL，页面提前预热免得起拍才拉、尾部被截
