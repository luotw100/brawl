"""狼人杀运行配置：字段即 `brawl werewolf` 的 CLI 参数。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import (
    BaseSettings,
    CliSettingsSource,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from brawl.games.werewolf import rules
from brawl.paths import REPO_ROOT

load_dotenv(REPO_ROOT / ".env")


class SettingsError(Exception):
    """面向用户的配置错误：CLI 捕获后只打印消息。"""


class WerewolfSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BRAWL_WEREWOLF_",
        env_nested_delimiter="__",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _config_file: ClassVar[Path | None] = None
    _cli_args: ClassVar[list[str] | None] = None

    board: Literal["pre-witch-hunter-idiot-12"] = rules.DEFAULT_BOARD
    cast: list[str] | None = None
    strategies: list[str] | None = None
    models: list[str] | None = None
    host_cast: str | None = None
    seed: int | None = None
    player: Literal["random", "opencode"] = "random"

    # OpenCode 由本 CLI 在 replay/_opencode 内托管；外部 server 连接模式已废弃。
    opencode_model: str = ""

    concurrency: int = 10
    replay: Path | None = None

    # 终端实时渲染：默认开，loop 每生成一条 sentence 就调用 ConsoleRenderer 输出到 stdout；
    # 批量/CI 跑 opencode 可用 --no-render 关闭。
    render: bool = True

    @property
    def players(self) -> int:
        return rules.board_by_id(self.board).players

    @model_validator(mode="after")
    def _check(self) -> WerewolfSettings:
        players = rules.board_by_id(self.board).players
        for field in ("cast", "strategies", "models"):
            val = getattr(self, field)
            if val is not None and len(val) != players:
                raise ValueError(f"{field} 长度 {len(val)} 必须等于 {players}（{self.board}）")
        if self.concurrency < 1:
            raise ValueError(f"concurrency 必须 >=1，收到 {self.concurrency}")
        if self.player == "opencode" and self.replay is None:
            raise ValueError("--player opencode 必须提供 --replay，用于隔离并保存 OpenCode 上下文")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [
            init_settings,
            CliSettingsSource(settings_cls, cli_parse_args=cls._cli_args if cls._cli_args is not None else [], cli_prog_name="brawl werewolf"),
            env_settings,
        ]
        cf = cls._config_file
        if cf is not None:
            if not cf.exists():
                raise SettingsError(f"配置文件不存在:{cf}")
            if cf.suffix != ".json":
                raise SettingsError(f"--config 仅支持 .json(收到:{cf})")
            sources.append(JsonConfigSettingsSource(settings_cls, json_file=cf))
        return tuple(sources)

    @classmethod
    def load(cls, cli_args: list[str], config_file: Path | None) -> WerewolfSettings:
        cls._cli_args = cli_args
        cls._config_file = config_file
        try:
            return cls()
        finally:
            cls._cli_args = None
            cls._config_file = None


class WerewolfResumeSettings(BaseModel):
    """续跑命令的全部输入。

    对局参数只能在 fresh 时决定；resume 边界只接受 replay 目录，
    玩家后端、模型、人设、流派和渲染设置全部由 meta.json 恢复。
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    replay: Path

    @classmethod
    def from_cli(cls, argv: list[str]) -> WerewolfResumeSettings:
        if (
            len(argv) != 3
            or argv[0] != "--resume"
            or argv[1] != "--replay"
            or not argv[2]
            or argv[2].startswith("-")
        ):
            raise SettingsError(
                "续跑只允许: brawl werewolf --resume --replay PATH；"
                "玩家、模型、人设、流派、并发度等元信息不能在续跑时修改"
            )
        return cls(replay=Path(argv[2]))
