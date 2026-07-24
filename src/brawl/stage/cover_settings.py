"""CoverSettings：`brawl cover` 的参数面。

生成封面只消费发布资源目录里的 `title.txt` 和 stage 当前背景图，固定产出三种比例：

```
<replay>/covers/4-3.png
<replay>/covers/3-4.png
<replay>/covers/16-9.png
```

边界一次性查清：目录、标题两行、期号、背景图、字体、输出覆盖策略；绘制函数拿到的是已解析好的
CoverJob 列表和节目期数文案，不再猜路径、不再挑字体。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import PrivateAttr, model_validator
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from brawl.paths import REPO_ROOT
from brawl.stage.stagecast import title_for

DEFAULT_BACKGROUND = REPO_ROOT / "web" / "public" / "scene.png"

StyleChoice = Literal["serif-gold", "sans-frame", "bold-ribbon"]


@dataclass(frozen=True)
class CoverSize:
    slug: str
    width: int
    height: int


@dataclass(frozen=True)
class CoverJob:
    size: CoverSize
    path: Path


COVER_SIZES = (
    CoverSize("4-3", 1600, 1200),
    CoverSize("3-4", 1200, 1600),
    CoverSize("16-9", 1920, 1080),
)

STYLE_FONTS: dict[StyleChoice, tuple[Path, ...]] = {
    "serif-gold": (
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
    ),
    "sans-frame": (
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"),
    ),
    "bold-ribbon": (
        Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"),
    ),
}
AVATAR_EXTS = (".png", ".webp", ".jpg", ".jpeg")


class CoverSettings(BaseSettings):
    """`brawl cover <replay_dir>`：把 `title.txt` 烧进舞台背景图生成三张封面。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_STAGE_COVER_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    dir: CliPositionalArg[Path]  # 发布资源 / replay 目录（含 title.txt 与 meta.json）
    output_dir: Path | None = None  # 输出目录；默认 <dir>/covers
    background: Path = DEFAULT_BACKGROUND  # stage 当前背景图
    style: StyleChoice = "serif-gold"  # 最终选定方案后固定默认值
    episode: str = "001"  # 节目期号；只填中间内容，封面显示为 AI {节目名}{episode}期
    force: bool = False  # 覆盖已存在输出

    _line1: str = PrivateAttr("")
    _line2: str = PrivateAttr("")
    _episode_text: str = PrivateAttr("")
    _font: Path = PrivateAttr(default_factory=Path)
    _avatars: tuple[Path, ...] = PrivateAttr(default_factory=tuple)
    _jobs: tuple[CoverJob, ...] = PrivateAttr(default_factory=tuple)

    @model_validator(mode="after")
    def _check(self) -> CoverSettings:
        if not self.dir.is_dir():
            raise ValueError(f"资源目录不存在：{self.dir}")
        title = self.dir / "title.txt"
        if not title.is_file():
            raise ValueError(f"缺 title.txt：{title}")
        lines = [line.strip() for line in title.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(lines) != 2:
            raise ValueError(f"title.txt 必须正好两行非空文本，当前 {len(lines)} 行：{title}")
        if not self.background.is_file():
            raise ValueError(f"背景图不存在：{self.background}")
        font = _resolve_font(self.style)
        avatars = _resolve_avatars(self.dir)
        jobs = tuple(CoverJob(size=size, path=self.cover_dir / f"{size.slug}.png") for size in COVER_SIZES)
        existing = [job.path for job in jobs if job.path.exists()]
        if existing and not self.force:
            names = ", ".join(str(path) for path in existing)
            raise ValueError(f"封面已存在：{names}（加 --force 覆盖）")
        self._line1, self._line2 = lines
        self._episode_text = f"AI {_show_title(self.dir)}{self.episode}期"
        self._font = font
        self._avatars = avatars
        self._jobs = jobs
        return self

    @property
    def line1(self) -> str:
        return self._line1

    @property
    def line2(self) -> str:
        return self._line2

    @property
    def episode_text(self) -> str:
        return self._episode_text

    @property
    def font(self) -> Path:
        return self._font

    @property
    def avatars(self) -> tuple[Path, ...]:
        return self._avatars

    @property
    def cover_dir(self) -> Path:
        return self.output_dir if self.output_dir is not None else self.dir / "covers"

    @property
    def jobs(self) -> tuple[CoverJob, ...]:
        return self._jobs

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
            init_settings,
            CliSettingsSource(
                settings_cls,
                cli_parse_args=cls._cli_args if cls._cli_args is not None else [],
                cli_prog_name="brawl cover",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> CoverSettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None


def _show_title(replay_dir: Path) -> str:
    """节目名从 replay 的 meta.game 派生（StageGame bundle 注册的显示名），不在壳里硬编码。"""
    meta_path = replay_dir / "meta.json"
    if not meta_path.is_file():
        raise ValueError(f"缺 meta.json：{meta_path}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"meta.json 不是合法 JSON：{meta_path}") from exc
    game = meta.get("game")
    if not isinstance(game, str) or not game:
        raise ValueError(f"meta.json 缺少 game 字段：{meta_path}")
    return title_for(game)


def _resolve_font(style: StyleChoice) -> Path:
    for path in STYLE_FONTS[style]:
        if path.is_file():
            return path
    raise ValueError(f"{style} 没有可用字体：{', '.join(str(path) for path in STYLE_FONTS[style])}")


def _resolve_avatars(replay_dir: Path) -> tuple[Path, ...]:
    """头像集由 replay 实际内容驱动：avatars/ 里所有 <座位号>.<图片后缀>，按座位号排序。

    不假设人数——几人的局就出几个头像（个别座位无烧入头像时自然缺席，布局按实际个数排）。
    """
    avatars_dir = replay_dir / "avatars"
    if not avatars_dir.is_dir():
        raise ValueError(f"缺 avatars 目录：{avatars_dir}")
    found = [
        (int(path.stem), path)
        for path in avatars_dir.iterdir()
        if path.is_file() and path.stem.isdigit() and path.suffix.lower() in AVATAR_EXTS
    ]
    if not found:
        raise ValueError(f"avatars 目录没有座位头像（<座位号>.png 等）：{avatars_dir}")
    return tuple(path for _, path in sorted(found))
