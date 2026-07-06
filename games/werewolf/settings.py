"""狼人杀运行配置：字段即 `brawl werewolf` 的 CLI 参数。"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from dotenv import load_dotenv
from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    CliSettingsSource,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from werewolf import rules

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


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

    # OpenCode headless server 连接参数。仅 player=opencode 时生效。
    opencode_url: str = "http://127.0.0.1:4096"
    opencode_username: str = "opencode"
    opencode_password_env: str = "OPENCODE_SERVER_PASSWORD"
    opencode_model: str = ""

    concurrency: int = 10
    replay: Path | None = None
    resume: bool = False

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
        if self.resume and self.replay is None:
            raise ValueError("--resume 需配合 --replay 指向那个要续跑的对局目录")
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


__all__ = ["SettingsError", "WerewolfSettings"]

