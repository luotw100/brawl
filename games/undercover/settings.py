"""谁是卧底的运行配置——它本身就是 CLI。

`UndercoverSettings` 是配置的唯一真源:字段即命令行参数(由 pydantic-settings
的 CliSettingsSource 自动派生,无需手写 argparse)。来源优先级:

    直接构造 kwargs > CLI --xxx > env BRAWL_UNDERCOVER_* > --config(JSON) > 默认

直接 kwargs 最高,是为了让代码/测试里 `UndercoverSettings(player="random", ...)`
照常生效;命令行入口走 `load(cli_args, config_file)`(见 run.py)。

人数/身份、人设与模型的座位映射、词、决策后端都在这里,全部可调。

注意:这里没有任何"计时/等待"配置——games 只做"对局快照生成器",尽快跑完一局
落成 replay,节奏(每阶段停留多久)是回放/直播层的事,不在生成器里 sleep。
"""

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

# 加载 games/.env(API key 等密钥放这里:不进命令行、不进配置文件、不入库)。
# 真实环境变量优先(override=False),故 CI/临时覆盖仍生效。路径写死到工程根,与 cwd 无关。
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class SettingsError(Exception):
    """面向用户的配置错误(--config 路径/格式不对等):main 只打印消息、不打 traceback。"""


class UndercoverSettings(BaseSettings):
    """一局谁是卧底的完整配置——同时也是 `brawl undercover` 的 CLI 参数面。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_UNDERCOVER_",
        env_nested_delimiter="__",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    # --- 运行期注入(非字段):--config 文件路径 + 喂给 CLI source 的 argv 切片 ---
    _config_file: ClassVar[Path | None] = None
    _cli_args: ClassVar[list[str] | None] = None

    players: int = 6
    undercover: int = 1
    blank: int = 0

    # 座位映射:cast[i] = 第 i 号座位的人设 id;strategies[i] = 流派 id;models[i] = 逻辑模型名。
    # 三者独立、长度均为 players;None 表示由边界用 registry 默认值补齐(流派默认全 standard)。
    cast: list[str] | None = None
    strategies: list[str] | None = None
    models: list[str] | None = None

    # (平民词, 卧底词);None = 从词库随机抽(方向也随机)
    word_pair: tuple[str, str] | None = None

    # 开局摇牌(身份/随机词)的随机种子;None = 每局不可复现。固定它即可复盘同一开局,
    # 也让"除模型外全一样"的对照实验把身份/词钉死。只影响开局摇定与平票随机,不影响 LLM 采样。
    seed: int | None = None

    # 决策后端:llm = 真模型在玩;random = 随机/可 mock
    player: Literal["llm", "random"] = "llm"

    # 回放产物目录:给了就把这局完整产物(events.jsonl + llm_calls.jsonl + meta.json)
    # 平铺写进该目录;不给=只渲染不落盘。目录已存在即报错(不覆盖、无 --force)。
    replay: Path | None = None

    @property
    def civilians(self) -> int:
        return self.players - self.undercover - self.blank

    @model_validator(mode="after")
    def _check(self) -> UndercoverSettings:
        if self.blank != 0:
            raise NotImplementedError("白板(blank)逻辑尚未实现,MVP 请保持 blank=0")
        if self.civilians < 2:
            raise ValueError(f"平民数过少:players={self.players} undercover={self.undercover} → 平民={self.civilians}(需 ≥2)")
        if self.undercover < 1:
            raise ValueError("至少要有 1 个卧底")
        for field in ("cast", "strategies", "models"):
            val = getattr(self, field)
            if val is not None and len(val) != self.players:
                raise ValueError(f"{field} 长度 {len(val)} 必须等于 players={self.players}")
        return self

    # ------------------------------------------------------------------
    # 来源优先级:直接 kwargs(init) > CLI > env > --config(JSON) > 默认。
    # --config 路径与 argv 在 load() 里经类变量注入。
    # ------------------------------------------------------------------
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
            init_settings,  # 直接构造的 kwargs 最高优先级(代码/测试照常生效)
            CliSettingsSource(
                settings_cls,
                cli_parse_args=cls._cli_args if cls._cli_args is not None else [],
                cli_prog_name="brawl undercover",
            ),
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

    # ------------------------------------------------------------------
    # 唯一入口:注入运行期 argv / config 文件后实例化。
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, cli_args: list[str], config_file: Path | None) -> UndercoverSettings:
        cls._cli_args = cli_args
        cls._config_file = config_file
        try:
            return cls()
        finally:
            cls._cli_args = None
            cls._config_file = None


__all__ = ["UndercoverSettings", "SettingsError"]
