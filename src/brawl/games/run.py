"""``brawl run`` / ``brawl resume``：跨玩法的统一开局与续跑边界。

一个参数面打所有板子：``--board`` 决定玩法、人数与角色构成（路由到玩法包），
其余参数（backend/seed/cast/models/…）全板通用——用户心智里只有"选哪块板子"。

板子与 resume 的登记发生在 cli 装配根（games 壳不 import 玩法包）；本模块只持有
注册表与统一 settings。各玩法包提供：rules 里的板子表（角色构成唯一真源）、
``run_game(settings)`` 与 ``resume_game(replay)`` 两个入口。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import SettingsConfigDict
from rich.console import Console

from brawl.runtime.cli_settings import CliSettings
from brawl.runtime.files import UnsafePathError, read_regular_bytes
from brawl.runtime.loop import run_async_main

_console = Console()


class SettingsError(Exception):
    """面向用户的配置错误：CLI 捕获后只打印消息、不打 traceback。"""


# ---------------------------------------------------------------- 注册表（cli 装配根登记）


@dataclass(frozen=True)
class BoardEntry:
    """一块可开局的板子：--board 的值 → 玩法路由 + 展示信息。"""

    id: str  # 板子 id（--board 的值），与玩法包 rules 板子表对齐
    game: str  # 玩法标识，与 meta.game / StageGame.id 对齐
    players: int  # 座位数：--cast/--models/--strategies 的长度基准
    title: str  # 一句话介绍（板子列表/错误提示用）
    run: Callable[[RunSettings], Coroutine[Any, Any, Any]]  # 开一局


_BOARDS: dict[str, BoardEntry] = {}
_RESUMERS: dict[str, Callable[[Path], Coroutine[Any, Any, Any]]] = {}


def register_board(entry: BoardEntry) -> None:
    if entry.id in _BOARDS:
        raise ValueError(f"板子重复登记：{entry.id}")
    _BOARDS[entry.id] = entry


def register_resumer(game: str, resume: Callable[[Path], Coroutine[Any, Any, Any]]) -> None:
    """按 meta.game 登记续跑入口（resume 只认玩法，与具体板子无关）。"""
    if game in _RESUMERS:
        raise ValueError(f"玩法 resume 重复登记：{game}")
    _RESUMERS[game] = resume


def _boards_blurb() -> str:
    return "、".join(f"{b.id}（{b.title}）" for b in _BOARDS.values())


# ---------------------------------------------------------------- brawl run


class RunSettings(CliSettings):
    """brawl run 的统一参数面：板子路由 + 全板通用的开局配置。"""

    CLI_PROG: ClassVar[str] = "brawl run"
    model_config = SettingsConfigDict(
        env_prefix="BRAWL_RUN_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    board: str = Field(description="板子（必填）：决定玩法、人数与角色构成；给错会列出全部可用板子")
    replay: Path = Field(description="对局产物目录（必填；已存在则拒绝，不覆盖）")
    backend: Literal["random", "opencode"] = Field(
        default="opencode",
        description="决策后端：opencode=真实模型对局；random=本地随机，零 key 试跑",
    )
    seed: int | None = Field(default=None, description="随机种子：固定即可复现同一开局")
    models: list[str] | None = Field(
        default=None, description="逐座位模型（逻辑名，见玩法 models.yaml）；缺省随机搭配"
    )
    cast: list[str] | None = Field(default=None, description="逐座位人设 id；缺省随机抽取不重复")
    strategies: list[str] | None = Field(default=None, description="逐座位打法流派 id；缺省随机")
    host_cast: str | None = Field(default=None, description="主持人人设 id；缺省 herald")
    concurrency: int = Field(default=10, description="模型并发上限；撞限流才需要调小")
    render: bool = Field(default=True, description="终端实时渲染；批量/CI 用 --no-render")

    @model_validator(mode="after")
    def _check(self) -> RunSettings:
        entry = _BOARDS.get(self.board)
        if entry is None:
            raise ValueError(f"未知板子：{self.board}（可用：{_boards_blurb()}）")
        for field in ("cast", "strategies", "models"):
            val = getattr(self, field)
            if val is not None and len(val) != entry.players:
                flag = field.replace("_", "-")
                raise ValueError(f"--{flag} 长度 {len(val)} 必须等于板子人数 {entry.players}（{self.board}）")
        if self.concurrency < 1:
            raise ValueError(f"--concurrency 必须 ≥1，收到 {self.concurrency}")
        return self


def main(argv: list[str]) -> int:
    """``brawl run``：解析统一参数面 → 按 --board 路由到玩法包开局。"""
    try:
        settings = RunSettings.load(argv)
    except ValidationError as exc:
        _console.print(f"[red]配置错误:[/red] {_validation_message(exc)}")
        return 1
    entry = _BOARDS[settings.board]  # load 已校验，必在册
    run_async_main(entry.run(settings))
    return 0


# ---------------------------------------------------------------- brawl resume


class ResumeSettings(CliSettings):
    """brawl resume 的全部输入：续跑只认 replay 目录，对局参数从 meta.json 恢复。"""

    CLI_PROG: ClassVar[str] = "brawl resume"
    model_config = SettingsConfigDict(
        env_prefix="BRAWL_RESUME_",
        cli_kebab_case=True,
        extra="forbid",
    )

    replay: Path = Field(
        description="要续跑的 replay 目录（必填）；玩法、模型、人设等全部从 meta.json 恢复，不可改"
    )


def _meta_game(replay: Path) -> str:
    """浅读 meta.json 的 game 字段做路由；完整 schema 校验由玩法包的续跑边界负责。"""
    meta_path = replay / "meta.json"
    try:
        raw = read_regular_bytes(meta_path)
    except (OSError, UnsafePathError) as exc:
        raise SettingsError(f"{replay} 不是可续跑的 replay（读不到 meta.json）：{exc}") from exc
    try:
        meta = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SettingsError(f"meta.json 不是合法 JSON：{meta_path}") from exc
    game = meta.get("game") if isinstance(meta, dict) else None
    if not isinstance(game, str) or not game:
        raise SettingsError(f"meta.json 缺少 game 字段：{meta_path}")
    if game not in _RESUMERS:
        raise SettingsError(f"未登记的玩法：{game}（已登记：{sorted(_RESUMERS)}）")
    return game


def resume_main(argv: list[str]) -> int:
    """``brawl resume --replay PATH``：按 meta.game 路由到玩法包续跑。"""
    try:
        settings = ResumeSettings.load(argv)
    except ValidationError as exc:
        _console.print(f"[red]配置错误:[/red] {_validation_message(exc)}")
        return 1
    try:
        game = _meta_game(settings.replay)
    except SettingsError as exc:
        _console.print(f"[red]配置错误:[/red] {exc}")
        return 1
    run_async_main(_RESUMERS[game](settings.replay))
    return 0


def _validation_message(exc: ValidationError) -> str:
    """pydantic 错误 → 单行中文（去掉 "Value error, " 前缀，带上字段定位）。"""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}：{msg}" if loc else msg)
    return "；".join(parts)
