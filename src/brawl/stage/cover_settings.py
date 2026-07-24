"""CoverSettings：`brawl cover` 的参数面。

生成封面只消费发布资源目录里的 `title.txt` 和 stage 当前背景图，固定产出三种比例：

```
<replay>/covers/4-3.png
<replay>/covers/3-4.png
<replay>/covers/16-9.png
```

样式已三案审结定稿 serif-gold，输出固定落 `<dir>/covers`——都不是用户旋钮。
已存在封面的覆盖确认在命令入口做人机边界（随机冷静码，见 cover.main），不在此校验。

边界一次性查清：目录、标题两行、期号、背景图、字体；绘制函数拿到的是已解析好的
CoverJob 列表和节目期数文案，不再猜路径、不再挑字体。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import Field, PrivateAttr, model_validator
from pydantic_settings import CliPositionalArg, SettingsConfigDict

from brawl.paths import REPO_ROOT
from brawl.runtime.cli_settings import CliSettings
from brawl.stage.stagecast import title_for

DEFAULT_BACKGROUND = REPO_ROOT / "web" / "public" / "scene.png"


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

# serif-gold 定稿字体的回退链（按可得性取第一个）。
FONT_CANDIDATES: tuple[Path, ...] = (
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
)
AVATAR_EXTS = (".png", ".webp", ".jpg", ".jpeg")


class CoverSettings(CliSettings):
    """把 title.txt 烧进舞台背景图，生成 4:3 / 3:4 / 16:9 三张封面到 <dir>/covers/。"""

    CLI_PROG: ClassVar[str] = "brawl cover"
    model_config = SettingsConfigDict(
        env_prefix="BRAWL_COVER_",
        cli_kebab_case=True,
        extra="forbid",
    )

    dir: CliPositionalArg[Path] = Field(description="发布资源 / replay 目录（含 title.txt 与 meta.json）")
    episode: str = Field(default="001", description="期号：封面显示为 AI <节目名>第 N 期，每期记得递增")
    background: Path = Field(default=DEFAULT_BACKGROUND, description="舞台背景图，一般不用动")

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
        self._line1, self._line2 = lines
        self._episode_text = f"AI {_show_title(self.dir)}{self.episode}期"
        self._font = _resolve_font()
        self._avatars = _resolve_avatars(self.dir)
        self._jobs = tuple(CoverJob(size=size, path=self.cover_dir / f"{size.slug}.png") for size in COVER_SIZES)
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
        """输出固定落 <dir>/covers（发布资源包约定），不开 --output-dir 旋钮。"""
        return self.dir / "covers"

    @property
    def jobs(self) -> tuple[CoverJob, ...]:
        return self._jobs


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


def _resolve_font() -> Path:
    for path in FONT_CANDIDATES:
        if path.is_file():
            return path
    raise ValueError(f"没有可用字体：{', '.join(str(path) for path in FONT_CANDIDATES)}")


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
