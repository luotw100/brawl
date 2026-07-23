"""ReplayMediaResolver：语义键 → 传输事实的解析边界。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brawl.stage.media import ReplayMediaResolver


@pytest.fixture
def replay_dir(tmp_path: Path) -> Path:
    tts = tmp_path / "tts"
    tts.mkdir()
    (tts / "manifest.json").write_text(json.dumps({
        "3.2": {"file": "a b.opus", "duration_ms": 1500},
        "4.host": {"file": "h.opus", "duration_ms": 800},
    }), encoding="utf-8")
    (tts / "a b.opus").write_bytes(b"x")
    (tts / "h.opus").write_bytes(b"x")
    avatars = tmp_path / "avatars"
    avatars.mkdir()
    (avatars / "2.png").write_bytes(b"x")
    return tmp_path


def test_audio_hit_and_url_quoting(replay_dir: Path) -> None:
    media = ReplayMediaResolver(replay_dir)
    url, seconds = media.audio("3.2")
    assert url == "/replay/tts/a%20b.opus"
    assert seconds == 1.5
    assert media.audio_path(url) == replay_dir / "tts" / "a b.opus"


def test_audio_miss_is_none_not_error(replay_dir: Path) -> None:
    assert ReplayMediaResolver(replay_dir).audio("99.9") is None


def test_no_manifest_is_empty_not_error(tmp_path: Path) -> None:
    media = ReplayMediaResolver(tmp_path)
    assert media.audio("3.2") is None
    assert media.durations() == {}


def test_avatar(replay_dir: Path) -> None:
    media = ReplayMediaResolver(replay_dir)
    assert media.avatar(2) == "/replay/avatars/2.png"
    assert media.avatar(3) is None


def test_durations_covers_all_manifest_entries(replay_dir: Path) -> None:
    assert ReplayMediaResolver(replay_dir).durations() == {
        "/replay/tts/a%20b.opus": 1.5,
        "/replay/tts/h.opus": 0.8,
    }


def test_audio_path_rejects_foreign_url(replay_dir: Path) -> None:
    with pytest.raises(ValueError, match="不是本 replay 的媒体"):
        ReplayMediaResolver(replay_dir).audio_path("/other/tts/x.opus")
