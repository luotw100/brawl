"""brawl cover：标题读取、边界校验和图片生成。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from core.stage.cover import render_covers
from core.stage.cover_settings import CoverSettings


def _write_resource_dir(tmp_path: Path, title: str = "梅林全程装死\n刺客一刀砍偏\n") -> Path:
    replay = tmp_path / "game1"
    replay.mkdir()
    (replay / "title.txt").write_text(title, encoding="utf-8")
    avatars = replay / "avatars"
    avatars.mkdir()
    for no in range(1, 11):
        img = Image.new("RGBA", (96, 96), (30 + no * 12, 40 + no * 9, 90 + no * 7, 255))
        img.save(avatars / f"{no}.png")
    return replay


def test_cover_settings_defaults_to_three_cover_pngs(tmp_path):
    replay = _write_resource_dir(tmp_path)

    settings = CoverSettings.load([str(replay)])

    assert [job.path for job in settings.jobs] == [
        replay / "covers" / "4-3.png",
        replay / "covers" / "3-4.png",
        replay / "covers" / "16-9.png",
    ]
    assert settings.line1 == "梅林全程装死"
    assert settings.line2 == "刺客一刀砍偏"
    assert settings.episode_text == "AI 阿瓦隆001期"
    assert settings.background.name == "scene.png"
    assert settings.font.is_file()
    assert len(settings.avatars) == 10


def test_cover_settings_fills_episode_number_into_label(tmp_path):
    replay = _write_resource_dir(tmp_path)

    settings = CoverSettings.load([str(replay), "--episode", "005"])

    assert settings.episode_text == "AI 阿瓦隆005期"


def test_cover_settings_accepts_episod_spelling(tmp_path):
    replay = _write_resource_dir(tmp_path)

    settings = CoverSettings.load([str(replay), "--episod", "第五局观察"])

    assert settings.episode_text == "AI 阿瓦隆第五局观察期"


def test_cover_settings_rejects_bad_title_lines(tmp_path):
    replay = _write_resource_dir(tmp_path, "只有一行\n")

    with pytest.raises(ValueError, match="title.txt 必须正好两行"):
        CoverSettings.load([str(replay)])


def test_cover_settings_rejects_existing_output_without_force(tmp_path):
    replay = _write_resource_dir(tmp_path)
    (replay / "covers").mkdir()
    (replay / "covers" / "4-3.png").write_bytes(b"old")

    with pytest.raises(ValueError, match="加 --force 覆盖"):
        CoverSettings.load([str(replay)])


def test_render_covers_writes_three_images_with_title_pixels(tmp_path):
    replay = _write_resource_dir(tmp_path)
    out = tmp_path / "covers"
    settings = CoverSettings.load([str(replay), "--output-dir", str(out), "--episode", "005"])

    paths = render_covers(settings)

    assert paths == [out / "4-3.png", out / "3-4.png", out / "16-9.png"]
    expected_sizes = [(1600, 1200), (1200, 1600), (1920, 1080)]
    for path, size in zip(paths, expected_sizes, strict=True):
        with Image.open(path) as img:
            assert img.size == size
    with Image.open(paths[2]) as img:
        colors = img.convert("RGB").getcolors(maxcolors=img.width * img.height)
    assert colors is not None
    assert any(r > 245 and g > 238 and b > 220 for _, (r, g, b) in colors)
    assert any(r > 245 and 185 < g < 230 and b < 95 for _, (r, g, b) in colors)
