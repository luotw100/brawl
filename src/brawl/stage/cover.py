"""brawl cover：把发布资源目录里的两行标题烧进舞台背景图。"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pydantic import ValidationError
from rich.console import Console

from brawl.stage.cover_settings import CoverSettings

WHITE = (255, 250, 238, 255)
YELLOW = (255, 211, 71, 255)
GOLD = (242, 204, 112, 225)
GOLD_DEEP = (214, 173, 87, 230)
DARK = (12, 8, 3, 225)
BRAND = "赛博物种观察日记"
_console = Console()


@dataclass(frozen=True)
class CoverTheme:
    gap_scale: float
    stroke_scale: float
    decorate: Callable[[Image.Image], None]


@dataclass(frozen=True)
class CoverLayout:
    brand_font_scale: float
    title_width: float
    title_anchor_y: float
    title_font_scale: float
    episode_width: float
    episode_anchor_y: float
    episode_font_scale: float
    episode_stroke_scale: float


THEMES: dict[str, CoverTheme] = {
    "serif-gold": CoverTheme(0.18, 0.055, lambda canvas: None),
    "sans-frame": CoverTheme(0.20, 0.050, lambda canvas: _decorate_frame(canvas)),
    "bold-ribbon": CoverTheme(0.14, 0.060, lambda canvas: _decorate_ribbon(canvas)),
}

LAYOUTS: dict[str, CoverLayout] = {
    "3-4": CoverLayout(0.068, 0.76, 0.39, 0.096, 0.74, 0.785, 0.081, 0.088),
    "4-3": CoverLayout(0.045, 0.68, 0.42, 0.108, 0.58, 0.815, 0.068, 0.080),
    "16-9": CoverLayout(0.040, 0.60, 0.46, 0.118, 0.48, 0.825, 0.064, 0.080),
}


def render_covers(settings: CoverSettings) -> list[Path]:
    paths = []
    for job in settings.jobs:
        canvas = _cover_background(settings.background, job.size.width, job.size.height)
        theme = THEMES[settings.style]
        layout = LAYOUTS[job.size.slug]
        theme.decorate(canvas)
        _draw_brand_label(canvas, settings.font, layout)
        _draw_title_and_avatars(canvas, settings.line1, settings.line2, settings.font, settings.avatars, theme, layout)
        _draw_episode_label(canvas, settings.episode_text, settings.font, theme, layout)
        job.path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(job.path)
        paths.append(job.path)
    return paths


def _cover_background(path: Path, width: int, height: int) -> Image.Image:
    bg = Image.open(path).convert("RGB")
    src_ratio = bg.width / bg.height
    dst_ratio = width / height
    if src_ratio > dst_ratio:
        crop_width = round(bg.height * dst_ratio)
        left = (bg.width - crop_width) // 2
        box = (left, 0, left + crop_width, bg.height)
    else:
        crop_height = round(bg.width / dst_ratio)
        top = (bg.height - crop_height) // 2
        box = (0, top, bg.width, top + crop_height)
    canvas = bg.crop(box).resize((width, height), Image.Resampling.LANCZOS).convert("RGBA")
    canvas.alpha_composite(Image.new("RGBA", canvas.size, (0, 0, 0, 54)))
    return canvas


def _decorate_frame(canvas: Image.Image) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    w, h = canvas.size
    inset = round(min(w, h) * 0.055)
    width = max(3, min(w, h) // 260)
    draw.rounded_rectangle(
        (inset, inset, w - inset, h - inset),
        radius=round(min(w, h) * 0.028),
        outline=(244, 208, 125, 170),
        width=width,
    )
    panel = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    pdraw = ImageDraw.Draw(panel, "RGBA")
    pdraw.rounded_rectangle(
        (round(w * 0.08), round(h * 0.24), round(w * 0.92), round(h * 0.86)),
        radius=round(min(w, h) * 0.025),
        fill=(8, 6, 3, 82),
        outline=(244, 208, 125, 95),
        width=max(2, width - 1),
    )
    canvas.alpha_composite(panel.filter(ImageFilter.GaussianBlur(radius=max(1, min(w, h) * 0.003))))


def _decorate_ribbon(canvas: Image.Image) -> None:
    draw = ImageDraw.Draw(canvas, "RGBA")
    w, h = canvas.size
    top = round(h * 0.26)
    bottom = round(h * 0.84)
    slant = round(w * 0.045)
    draw.polygon(
        ((0, top + slant), (w, top), (w, bottom - slant), (0, bottom)),
        fill=(6, 4, 2, 118),
    )
    draw.line((0, top + slant, w, top), fill=(244, 208, 125, 150), width=max(3, h // 260))
    draw.line((0, bottom, w, bottom - slant), fill=(244, 208, 125, 130), width=max(3, h // 300))


def _draw_brand_label(canvas: Image.Image, font_path: Path, layout: CoverLayout) -> None:
    draw = ImageDraw.Draw(canvas)
    w, h = canvas.size
    font = ImageFont.truetype(str(font_path), size=round(min(w * layout.brand_font_scale, h * 0.060)))
    margin_x = round(w * 0.025)
    margin_y = round(h * 0.030)
    _draw_small_text(draw, (margin_x, margin_y), BRAND, font, WHITE)


def _draw_small_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    color: tuple[int, int, int, int],
) -> None:
    stroke = _small_stroke(font)
    x, y = xy
    offset = max(3, round(font.size * 0.07))
    draw.text((x + offset, y + offset), text, font=font, fill=(0, 0, 0, 150), stroke_width=stroke, stroke_fill=DARK)
    draw.text((x, y), text, font=font, fill=color, stroke_width=stroke, stroke_fill=DARK)


def _small_stroke(font: ImageFont.FreeTypeFont) -> int:
    return max(4, round(font.size * 0.075))


def _draw_title_and_avatars(
    canvas: Image.Image,
    line1: str,
    line2: str,
    font_path: Path,
    avatars: tuple[Path, ...],
    theme: CoverTheme,
    layout: CoverLayout,
) -> None:
    draw = ImageDraw.Draw(canvas)
    max_width = int(canvas.width * layout.title_width)
    font = _fit_font(draw, font_path, (line1, line2), max_width, canvas.height, theme, layout.title_font_scale)
    stroke = _stroke_width(font, theme)
    gap = round(font.size * theme.gap_scale)
    title_width = _title_width(draw, (line1, line2), font, stroke)
    line_images = tuple(_text_image(line, font, color, stroke, title_width) for line, color in (
        (line1, WHITE),
        (line2, YELLOW),
    ))
    total_height = line_images[0].height + gap + line_images[1].height
    title_top = round(canvas.height * layout.title_anchor_y - total_height / 2)
    _draw_avatar_rows(canvas, avatars, title_top, title_top + total_height, title_width)
    y = title_top
    for image in line_images:
        canvas.alpha_composite(image, (round((canvas.width - title_width) / 2), y))
        y += image.height + gap


def _draw_episode_label(
    canvas: Image.Image,
    label: str,
    font_path: Path,
    theme: CoverTheme,
    layout: CoverLayout,
) -> None:
    draw = ImageDraw.Draw(canvas)
    max_width = round(canvas.width * layout.episode_width)
    font = _fit_text_font(
        draw,
        font_path,
        label,
        max_width,
        round(canvas.height * layout.episode_font_scale),
        layout.episode_stroke_scale,
    )
    stroke = max(3, round(font.size * layout.episode_stroke_scale))
    label_width = min(max_width, _tracked_natural_width(draw, label, font, stroke))
    image = _text_image(label, font, GOLD, stroke, label_width)
    x = round((canvas.width - image.width) / 2)
    y = round(canvas.height * layout.episode_anchor_y - image.height / 2)
    canvas.alpha_composite(image, (x, y))


def _title_width(
    draw: ImageDraw.ImageDraw,
    lines: tuple[str, str],
    font: ImageFont.FreeTypeFont,
    stroke: int,
) -> int:
    return max(_tracked_natural_width(draw, line, font, stroke) for line in lines)


def _text_image(
    line: str,
    font: ImageFont.FreeTypeFont,
    color: tuple[int, int, int, int],
    stroke: int,
    target_width: int,
) -> Image.Image:
    measure = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(measure)
    box = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
    shadow_offset = max(4, round(font.size * 0.045))
    natural_width = _tracked_natural_width(draw, line, font, stroke)
    height = box[3] - box[1] + shadow_offset
    image = Image.new("RGBA", (target_width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    extra = max(0, target_width - natural_width)
    tracking = min(extra / max(1, len(line) - 1), font.size * 0.08)
    actual_width = natural_width + tracking * max(0, len(line) - 1)
    x = (target_width - actual_width) / 2 - box[0]
    for char in line:
        char_box = draw.textbbox((0, 0), char, font=font, stroke_width=stroke)
        xy = (round(x), -box[1])
        shadow_xy = (xy[0] + shadow_offset, xy[1] + shadow_offset)
        draw.text(shadow_xy, char, font=font, fill=(0, 0, 0, 130), stroke_width=stroke, stroke_fill=DARK)
        draw.text(xy, char, font=font, fill=color, stroke_width=stroke, stroke_fill=DARK)
        x += (char_box[2] - char_box[0]) + tracking
    return image


def _tracked_natural_width(
    draw: ImageDraw.ImageDraw,
    line: str,
    font: ImageFont.FreeTypeFont,
    stroke: int,
) -> int:
    shadow_offset = max(4, round(font.size * 0.045))
    return sum(
        box[2] - box[0]
        for char in line
        for box in (draw.textbbox((0, 0), char, font=font, stroke_width=stroke),)
    ) + shadow_offset


def _draw_avatar_rows(
    canvas: Image.Image,
    avatars: tuple[Path, ...],
    title_top: int,
    title_bottom: int,
    target_width: int,
) -> None:
    """头像分两排夹住标题：上排 ceil(N/2) 个贴标题上沿、其余贴下沿。

    每排按实际个数定尺寸与步距——10 人局与旧版 2×5 布局逐像素一致，其它人数自然收放。
    """
    gap_ratio = 0.45
    top_count = (len(avatars) + 1) // 2
    for row, edge, above in (
        (avatars[:top_count], title_top, True),
        (avatars[top_count:], title_bottom, False),
    ):
        if not row:
            continue
        n = len(row)
        size = round(target_width / (n + (n - 1) * gap_ratio))
        x0 = round((canvas.width - target_width) / 2)
        step = (target_width - size) / max(n - 1, 1)
        avatar_gap = round(size * 0.28)
        y = edge - avatar_gap - size if above else edge + avatar_gap
        for idx, avatar in enumerate(row):
            _draw_avatar(canvas, avatar, round(x0 + idx * step), y, size)


def _draw_avatar(canvas: Image.Image, path: Path, x: int, y: int, size: int) -> None:
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile, "RGBA")
    draw.ellipse((0, 0, size - 1, size - 1), fill=(18, 14, 8, 235), outline=GOLD_DEEP, width=max(3, size // 22))
    img = Image.open(path).convert("RGBA")
    fitted = ImageOps.contain(img, (round(size * 0.70), round(size * 0.70)), Image.Resampling.LANCZOS)
    tile.alpha_composite(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
    canvas.alpha_composite(tile, (x, y))


def _fit_font(
    draw: ImageDraw.ImageDraw,
    font_path: Path,
    lines: tuple[str, str],
    max_width: int,
    canvas_height: int,
    theme: CoverTheme,
    font_scale: float,
) -> ImageFont.FreeTypeFont:
    size = round(canvas_height * font_scale)
    min_size = 24
    while size > min_size:
        font = ImageFont.truetype(str(font_path), size=size)
        stroke = _stroke_width(font, theme)
        widest = max(_tracked_natural_width(draw, line, font, stroke) for line in lines)
        if widest <= max_width:
            return font
        size -= 4
    return ImageFont.truetype(str(font_path), size=min_size)


def _fit_text_font(
    draw: ImageDraw.ImageDraw,
    font_path: Path,
    text: str,
    max_width: int,
    start_size: int,
    stroke_scale: float,
) -> ImageFont.FreeTypeFont:
    size = start_size
    min_size = 20
    while size > min_size:
        font = ImageFont.truetype(str(font_path), size=size)
        stroke = max(3, round(font.size * stroke_scale))
        if _tracked_natural_width(draw, text, font, stroke) <= max_width:
            return font
        size -= 4
    return ImageFont.truetype(str(font_path), size=min_size)


def _stroke_width(font: ImageFont.FreeTypeFont, theme: CoverTheme) -> int:
    return max(3, round(font.size * theme.stroke_scale))


def main(argv: list[str]) -> int:
    try:
        settings = CoverSettings.load(argv)
    except ValidationError as e:
        msg = "；".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        _console.print(f"[red]配置错误:[/red] {msg}")
        return 1
    paths = render_covers(settings)
    _console.print(f"[bold green]✓ 已生成[/bold green] {len(paths)} 张封面")
    for path in paths:
        _console.print(f"  {path}")
    return 0


def main_cli() -> None:
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    main_cli()
