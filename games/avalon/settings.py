"""阿瓦隆的运行配置——它本身就是 CLI。

`AvalonSettings` 是配置的唯一真源:字段即命令行参数(由 pydantic-settings
的 CliSettingsSource 自动派生,无需手写 argparse)。来源优先级:

    直接构造 kwargs > CLI --xxx > env BRAWL_AVALON_* > --config(JSON) > 默认

直接 kwargs 最高,是为了让代码/测试里 `AvalonSettings(players=5, ...)` 照常生效;
命令行入口走 `load(cli_args, config_file)`(见 run.py)。

阵营构成与每任务队伍人数不在这里配——那是规则表(rules.py),由 players 唯一决定。
角色面:梅林 + 刺客必上;派西维尔/莫甘娜/莫德雷德/奥伯伦是可选开关(占用本阵营名额,
其余名额由忠臣/爪牙补满)。湖中仙女是流程模块，不是角色牌。

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

from avalon import rules

# 加载 games/.env(API key 等密钥放这里:不进命令行、不进配置文件、不入库)。
# 真实环境变量优先(override=False),故 CI/临时覆盖仍生效。路径写死到工程根,与 cwd 无关。
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class SettingsError(Exception):
    """面向用户的配置错误(--config 路径/格式不对等):main 只打印消息、不打 traceback。"""


class AvalonSettings(BaseSettings):
    """一局阿瓦隆的完整配置——同时也是 `brawl avalon` 的 CLI 参数面。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_AVALON_",
        env_nested_delimiter="__",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    # --- 运行期注入(非字段):--config 文件路径 + 喂给 CLI source 的 argv 切片 ---
    _config_file: ClassVar[Path | None] = None
    _cli_args: ClassVar[list[str] | None] = None

    players: int = 10

    # 可选特殊角色开关(梅林/刺客必上,不可关)。各占一个本阵营名额,其余名额忠臣/爪牙补满。
    # 默认全关=最简局(仅梅林+刺客);要"标准全角色局"按需逐个开,如 10 人全开:
    # 6蓝=梅林+派西维尔+4忠臣,4红=刺客+莫甘娜+莫德雷德+奥伯伦(线上即这样显式起局)。
    percival: bool = False
    morgana: bool = False  # 需要派西维尔在场(否则假梅林无人可骗)
    mordred: bool = False
    oberon: bool = False

    # 湖中仙女(可选流程模块,非角色牌):第 2/3/4 个任务结束后,令牌持有者查验一人阵营
    # 并公开发言(可撒谎),令牌传给被查者。官方推荐 7 人以上启用,默认关、按需 --lady 开。
    lady: bool = False

    # 座位映射:cast[i] = 第 i 号座位的人设 id;strategies[i] = 流派 id;models[i] = 逻辑模型名。
    # 三者独立、长度均为 players;None=由边界随机采样补齐(均受 --seed 复现):cast 无放回(各座位
    # 人设互异,免撞音色/网名),strategies 有放回,models 有放回但抽中过后降权。详见 validation 的解析器。
    cast: list[str] | None = None
    strategies: list[str] | None = None
    models: list[str] | None = None

    # 主持人人设 id(cast.yaml);None=默认 herald。主持人台词由代码固定合成,persona 的
    # voice 对他不生效——此选择只决定显示名与将来 TTS 的音色映射,经边界校验写进 meta.setup。
    host_cast: str | None = None

    # 开局摇牌(角色分配/首任队长)的随机种子;None = 每局不可复现。固定它即可复盘同一开局。
    seed: int | None = None

    # 决策后端:random = 本地随机 agent(零 key 跑通全流程);opencode = 走 OpenCode headless server
    player: Literal["random", "opencode"] = "random"

    # OpenCode 由本 CLI 在 replay/_opencode 内托管；外部 server 连接模式已废弃。
    # 会话使用的模型格式为 providerID/modelID,例如 opencode-go/deepseek-v4-pro；
    # 空字符串表示使用 models.yaml 的逐座位映射，仍缺省时才让 server 选默认模型。
    opencode_model: str = ""

    # 同时行动阶段(组队表决/任务出牌)的并发上限:全员 asyncio.gather 并行调模型,
    # 用信号量限流。默认 10 = 最大桌全并发;撞限流时调小,不用改代码。
    concurrency: int = 10

    # 回放产物目录:给了就写本局完整产物;opencode 模式还会在 _opencode/ 保存隔离工作区、
    # 会话 DB 与日志。random 不给=只渲染不落盘。目录已存在即报错(不覆盖、无 --force)。
    replay: Path | None = None

    # 终端实时渲染：默认开，loop 每生成一条 sentence 就调用 ConsoleRenderer 输出到 stdout；
    # 批量/CI 跑 opencode 可用 --no-render 关闭。
    render: bool = True

    @property
    def evil(self) -> int:
        return rules.EVIL_COUNT[self.players]

    @property
    def good(self) -> int:
        return self.players - self.evil

    @property
    def evil_specials(self) -> int:
        """红方特殊角色占用的名额:刺客必上 + 三个可选开关。"""
        return 1 + self.morgana + self.mordred + self.oberon

    @model_validator(mode="after")
    def _check(self) -> AvalonSettings:
        if self.players not in rules.TEAM_SIZES:
            raise ValueError(f"阿瓦隆支持 {min(rules.TEAM_SIZES)}~{max(rules.TEAM_SIZES)} 人,收到 players={self.players}")
        if self.morgana and not self.percival:
            raise ValueError("莫甘娜需要派西维尔在场(假梅林得有人可骗):请同时开 --percival 或关 --morgana")
        if self.evil_specials > self.evil:
            raise ValueError(f"红方特殊角色 {self.evil_specials} 个超出 {self.players} 人局的红方名额 {self.evil}:关掉部分开关或加人数")
        for field in ("cast", "strategies", "models"):
            val = getattr(self, field)
            if val is not None and len(val) != self.players:
                raise ValueError(f"{field} 长度 {len(val)} 必须等于 players={self.players}")
        if self.concurrency < 1:
            raise ValueError(f"concurrency 必须 ≥1,收到 {self.concurrency}")
        if self.player == "opencode" and self.replay is None:
            raise ValueError("--player opencode 必须提供 --replay，用于隔离并保存 OpenCode 上下文")
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
                cli_prog_name="brawl avalon",
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
    def load(cls, cli_args: list[str], config_file: Path | None) -> AvalonSettings:
        cls._cli_args = cli_args
        cls._config_file = config_file
        try:
            return cls()
        finally:
            cls._cli_args = None
            cls._config_file = None


__all__ = ["AvalonSettings", "SettingsError"]
