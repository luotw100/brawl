"""CliSettings：全库 BaseSettings 的公共骨架——settings 即 CLI，flag 自动派生。

各命令的 settings 类只需声明字段（带 ``Field(description=...)`` 中文说明）与
``CLI_PROG``，CLI source 装配与 ``load()`` 入口在此共享一份，不再逐类手抄。
来源优先级：直接构造 kwargs > CLI --xxx > env > 默认。
"""

from __future__ import annotations

from typing import ClassVar

from pydantic_settings import (
    BaseSettings,
    CliSettingsSource,
    PydanticBaseSettingsSource,
)


class CliSettings(BaseSettings):
    """带 CLI 解析的 settings 基类。子类约定：``CLI_PROG`` 填 ``brawl <命令>``。"""

    CLI_PROG: ClassVar[str] = "brawl"

    # 运行期注入（非字段）：喂给 CLI source 的 argv 切片。
    _cli_args: ClassVar[list[str] | None] = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,  # 直接构造的 kwargs 最高优先级（代码/测试照常生效）
            CliSettingsSource(
                settings_cls,
                cli_parse_args=cls._cli_args if cls._cli_args is not None else [],
                cli_prog_name=cls.CLI_PROG,
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]):
        """唯一入口：注入运行期 argv 后实例化。"""
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None
