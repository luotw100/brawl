"""演出投影契约：玩法包 ↔ 演出壳之间唯一的缝。

一个玩法向壳交付一个 `StageGame` bundle（在玩法包内装配、经 `register` 登记，
壳按 `meta.game` 用 `game_for` 取用）。壳从不 import 任何玩法包。

`Stagecast` 是纯投影：读存档（sentences + meta），经 `MediaResolver` 把语义键
（call_id / 座位号）解成传输事实（URL/时长），产出指令流。它不碰节奏
（plan/playplan 的事）、不碰 URL 构造（resolver 的事）、不写盘。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from core.runtime.types import Sentence
from core.stage.protocol import BaseScene, Directive, FeedItem


class MediaResolver(Protocol):
    """壳注入的媒体解析：语义键 → 传输事实。stagecast 只见此接口。"""

    def audio(self, call_id: str) -> tuple[str, float] | None:
        """call_id（`{seq}.{speaker}`）→ (audio_url, 秒)；manifest 无此条 = None（无声是有效状态）。"""
        ...

    def avatar(self, seat: int) -> str | None:
        """座位号 → 头像 URL；该座位无烧入头像 = None（前端回退 hash 色块）。"""
        ...


class Stagecast(Protocol):
    """一个玩法的演出投影：存档 → 舞台指令流。纯函数；不写盘、不碰节奏。"""

    def stagecast(self, sentences: list[Sentence], meta: dict[str, Any], media: MediaResolver) -> list[Directive]: ...


@dataclass(frozen=True)
class StageGame:
    """一个玩法向演出壳交付的全部能力。无状态：随局数据经入参传入。"""

    id: str  # 玩法标识，与 meta.game 对齐（registry 据此派发）
    stagecast: Stagecast
    new_scene: Callable[[], BaseScene]
    apply: Callable[[BaseScene, FeedItem], None]  # 展示态 reducer（feed 入队由引擎统一做）
    summary: Callable[[Directive, BaseScene], tuple[str, str, str]]  # 一拍 → 录制进度行
    is_opening_anchor: Callable[[Directive], bool]  # 录制首帧门
    terminal: Callable[[Directive], bool]  # 终局指令判定（录制收尾/准入闸共用）
    plan_path: Path  # 该玩法的节奏配方 YAML


_REGISTRY: dict[str, StageGame] = {}


def register(game: StageGame) -> None:
    """登记一个玩法 bundle（装配根 = CLI 入口层；重复登记视为装配错误）。"""
    if game.id in _REGISTRY:
        raise ValueError(f"玩法重复登记：{game.id}")
    _REGISTRY[game.id] = game


def game_for(game_id: str) -> StageGame:
    try:
        return _REGISTRY[game_id]
    except KeyError:
        raise ValueError(f"未登记的玩法：{game_id}（已登记：{sorted(_REGISTRY)}）") from None
