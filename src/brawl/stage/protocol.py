"""舞台协议（框架信封）：服务端 ↔ 页面的消息形状里**与玩法无关**的那一层。

设计（"电视信号"语义）：source of truth 只有引擎里的一份 Scene。WS 频道上只有一种
消息——`StateMsg`，每一拍（以及任何状态变化时）推送完整的当前舞台状态；客户端是纯
渲染函数，零状态、零折叠逻辑，首次连接与第 N 拍收到的是同一种东西，重连即恢复。

游戏无关边界：这个文件只放"信封"——指令基类、主持台词、旁白、FeedItem、Scene 基类、
StateMsg。**具体玩法的指令形状与 Scene 字段在 <game>/protocol.py，子类化这里的基类。**
框架引擎只认基类，不认任何具体指令——加玩法即并列一个玩法包，引擎零分叉。

序列化边界：`FeedItem.directive` 与 `StateMsg.scene` 声明为基类但用 `SerializeAsAny`
透传——引擎持有的是具体子类（如 AvalonScene / Speech），dump 时按子类的真实字段
全量序列化给页面；框架自身只序列化、从不反解这些 Scene/Directive。

音频 join：指令的 `call_id` 是它与 TTS manifest 的语义键（`{seq}.{speaker}`，由玩法
stagecast 在投影时填入）；`audio_url` 由壳的 MediaResolver 解析填充，玩法不构造 URL。

时间对齐：FeedItem 带 `started_at`（服务端 epoch 秒）+ `duration` + `clock_running`，
StateMsg 带服务端当前时刻 `ts`——客户端在 clock_running 后用 (ts - started_at)
推出"这句话已经演到第几秒"，打字机从正确字数续接、音频 `currentTime` 对齐，
刷新/中途加入都无感。录制首帧门可先下发 clock_running=False 的静止首帧，抓屏就绪后
再启动同一拍的时钟，避免首帧预览把动画提前跑掉。
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field, SerializeAsAny

# ---------------------------------------------------------------- 指令基类（框架只认这层）


class Directive(BaseModel):
    """舞台指令基类：框架只依赖 `kind` 做计划编译与页面渲染分派。
    `call_id` 是 TTS 语义键（玩法 stagecast 填），`audio_url` 是主语音载荷（壳解析填），
    PlayPlan 会把 audio_url 投影成 tracks。具体玩法的指令子类化它、自带各自字段；
    框架引擎从不 match 具体子类。"""

    kind: str
    call_id: str | None = None
    audio_url: str | None = None


class HostSpeech(Directive):
    """主持人台词：天然的过场与报幕。各玩法共用一个主持人，故它是框架指令；
    `beat` 字符串的取值各玩法自定义（哪些 beat 算 lead-in 由玩法 plan.yaml 的
    host.placements 决定）。"""

    kind: Literal["host"] = "host"
    name: str  # 主持人显示名（meta.setup.host_name，生成时快照进 meta）
    beat: str  # 玩法自定义的报幕节点名（avalon：opening/propose_intro/.../closing）
    text: str


class GameResult(BaseModel):
    """一局终局的中性摘要（谁赢、为何赢），不认各玩法 winner 的具体取值。"""

    winner: str
    win_reason: str


class NarrationCue(BaseModel):
    """挂在一拍视觉之下的主持旁白。at 是拍内偏移秒；duration 是旁白音频预算。"""

    name: str
    beat: str
    text: str
    audio_url: str | None = None
    at: float = 0.0
    duration: float = 0.0


class AudioTrack(BaseModel):
    """一拍内的确定性音轨计划。直播页和录制都只消费这张轨道表。

    start/duration 已经是拍内秒数，并已按 engine speed 压缩；播放侧把音频 playbackRate 同步设为
    speed。sfx 轨使用 /sfx/<name>.opus，replay 人声使用 <url_prefix>/tts/<file>。
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


# ---------------------------------------------------------------- 舞台状态


class FeedItem(BaseModel):
    """一拍已上演的 directive + 它的演出时间窗。directive 声明为基类、`SerializeAsAny` 透传具体子类。"""

    seq: int
    directive: SerializeAsAny[Directive]
    started_at: float  # 服务端 epoch 秒
    duration: float
    clock_running: bool = True  # False=首帧预览/暂停态；客户端固定 elapsed=0 且不播音频
    speed: float = 1.0  # 时间轴倍速：duration 已按它压缩；播放侧据此把音频 playbackRate 提速贴合
    content_at: float = 0.0  # 主内容拍内偏移：前置主持旁白先说完，玩家发言再开始
    narrations: list[NarrationCue] = Field(default_factory=list)
    tracks: list[AudioTrack] = Field(default_factory=list)


class BaseScene(BaseModel):
    """舞台快照基类（唯一的 source of truth，engine 持有并折叠）。

    框架只依赖 `game`（页面据此挑渲染器）与 `feed`（窗口裁剪 / 时间对齐）这两个字段；
    座位、任务进度等玩法状态在 <game>/protocol.py 的子类里。`SerializeAsAny` 保证
    引擎持有的具体子类按真实字段全量下发。"""

    game: str = ""
    feed: list[FeedItem] = Field(default_factory=list)  # 服务端硬上限 FEED_CAP；下发时按窗口旋钮裁


class StateMsg(BaseModel):
    """频道唯一的消息：当前时刻的完整舞台状态。"""

    type: Literal["state"] = "state"
    ts: float = Field(default_factory=time.time)  # 服务端当前时刻（客户端对钟用）
    phase: Literal["playing"] = "playing"
    scene: SerializeAsAny[BaseScene]
    prefetch: list[str] = Field(default_factory=list)  # 接下来几拍的音频 URL，页面提前预热免得起拍才拉、尾部被截
