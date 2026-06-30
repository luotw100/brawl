"""玩法插件契约：框架引擎 ↔ 某个玩法之间唯一的缝。

引擎（engine/record）只通过这组钩子认识一局对局，**从不 import 或 match 任何具体指令类型**。
加一个玩法 = 在 games/<game>/ 写一个满足 `GamePlugin` 的实现，并在 registry 登记；引擎零分叉。

钩子分三组：
- 适配/折叠：`adapt`（replay 事件→舞台指令）、`apply`（把一拍折进玩法 Scene）、`new_scene`。
- 节奏：`content_seconds`/`tail_seconds`（每条指令的视觉/朗读秒数与尾部气口，未计音频/倍速——
  音频地板与 speed 由引擎统一套）、`reveal_paced_by_narration`/`speech_paced`（两条编排谓词，
  见 engine._duration / _stage_beats）、`lead_in_beats`（哪些主持 beat 当前置报幕先播完）。
- 框架挂钩与演出：`is_opening_anchor`（哪条指令是"首帧门"，录制等它 paint 再开抓）、
  `result`（终局指令→中性 GameResult 喂幕间）、`cues`（一拍叠哪些音效）、`summary`（录制进度条一行）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from server.protocol import BaseScene, Directive, FeedItem, GameResult


@dataclass(frozen=True)
class AdaptContext:
    """adapt 一条事件所需的随局上下文（全部由 server.replay 的通用读取器在边界备齐）。"""

    replay_id: str
    tts: dict[str, dict[str, Any]]  # call_id → {file, duration_ms, ...}（音频 URL + 节奏时长同源）
    names: dict[str, str]  # persona id → 显示名
    host_name: str = "主持人"
    avatars: dict[int, str] = field(default_factory=dict)  # 座位号 → 厂商头像 URL
    model_labels: dict[str, str] = field(default_factory=dict)  # 逻辑模型名 → 对外展示名


class GamePlugin(Protocol):
    """一个玩法向舞台框架交付的全部能力。无状态：所有随局数据经 AdaptContext / 入参传入。"""

    id: str  # 玩法标识，与 meta.game 对齐（registry 据此派发）

    # ---- 适配 / 折叠 ----
    def new_scene(self) -> BaseScene:
        """造一张该玩法的空白 Scene（每局开播、以及待机底图都用它）。"""
        ...

    def adapt(self, event: dict[str, Any], ctx: AdaptContext) -> Directive | None:
        """一条 replay 事件 → 一条舞台指令；返回 None 表示这条不演（如上帝视角下复述身份的拍）。
        未知事件类型应直接抛——契约漂移要立刻暴露，不静默吞。"""
        ...

    def apply(self, scene: BaseScene, item: FeedItem) -> None:
        """把一拍折进玩法 Scene（feed 入队与窗口护栏由引擎统一做，这里只折玩法状态）。"""
        ...

    # ---- 节奏 ----
    def content_seconds(self, directive: Directive, pace: float) -> float:
        """这条指令的主内容秒数（视觉动画/朗读，未计音频地板与 speed）。pace 0~1 线性缩放。"""
        ...

    def tail_seconds(self, directive: Directive, pace: float) -> float:
        """这条指令演完后的尾部气口秒数（发言间留白；未计 speed）。无尾巴返回 0。"""
        ...

    def reveal_paced_by_narration(self, directive: Directive) -> bool:
        """这条指令是否是"动画时长由主持旁白撑满"的揭晓拍（发牌/表决/任务开牌）。"""
        ...

    def speech_paced(self, directive: Directive) -> bool:
        """这条指令是否带玩家口白（其后的主持旁白要等口白说完再起）。"""
        ...

    def lead_in_beats(self) -> set[str]:
        """哪些主持 beat 作为 lead-in 先播完、再让玩家正文开始（其余 host 事件压在相邻视觉拍上）。"""
        ...

    # ---- 框架挂钩 / 演出 ----
    def is_opening_anchor(self, directive: Directive) -> bool:
        """这条指令是否是开场"首帧门"——录制据此等页面 paint 出这一帧后才开抓、对齐视频零点。"""
        ...

    def result(self, directive: Directive) -> GameResult | None:
        """若这条是终局指令，给出中性的胜负摘要（喂幕间页）；否则 None。"""
        ...

    def cues(self, directive: Directive, duration: float) -> list[tuple[str, float]]:
        """一拍 → [(音效名, 拍内偏移秒)]。无音效的拍返回空。"""
        ...

    def summary(self, directive: Directive, scene: BaseScene) -> tuple[str, str, str]:
        """一拍 → (标签, 颜色, 详情)，给录制进度条打成一行。"""
        ...
