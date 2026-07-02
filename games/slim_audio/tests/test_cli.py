"""slim-audio：危险确认、静音替换与 manifest 时长同步。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import soundfile as sf
from pydantic import ValidationError
from rich.console import Console

from slim_audio.cli import SlimAudioSettings, build_plan, execute, slim_audio


def _replay_dir(tmp_path: Path) -> Path:
    replay = tmp_path / "replay"
    tts = replay / "tts"
    tts.mkdir(parents=True)
    (tts / "a.opus").write_bytes(b"old-a")
    (tts / "b.opus").write_bytes(b"old-b")
    (tts / "manifest.json").write_text(
        json.dumps(
            {
                "a": {"file": "a.opus", "duration_ms": 9000},
                "b": {"file": "b.opus", "duration_ms": 4500},
                "wav": {"file": "c.wav", "duration_ms": 8000},
            }
        ),
        encoding="utf-8",
    )
    return replay


def _console() -> Console:
    return Console(file=None, force_terminal=False, width=120)


def test_time_defaults_to_three_seconds(tmp_path: Path) -> None:
    settings = SlimAudioSettings.load([str(_replay_dir(tmp_path))])
    assert settings.time == 3.0


def test_time_accepts_cli_value(tmp_path: Path) -> None:
    settings = SlimAudioSettings.load([str(_replay_dir(tmp_path)), "--time", "1.5"])
    assert settings.time == 1.5


@pytest.mark.parametrize("value", ["0", "-1"])
def test_time_must_be_positive(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValidationError):
        SlimAudioSettings.load([str(_replay_dir(tmp_path)), "--time", value])


def test_execute_replaces_opus_and_updates_manifest(tmp_path: Path) -> None:
    replay = _replay_dir(tmp_path)
    plan = SlimAudioSettings.load([str(replay), "--time", "1.25"])

    result = execute(build_plan(plan))

    assert result.files == 2
    for path in sorted((replay / "tts").glob("*.opus")):
        data, sr = sf.read(path)
        assert sr == 24_000
        assert len(data) == 30_000
    manifest = json.loads((replay / "tts" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["a"]["duration_ms"] == 1250
    assert manifest["b"]["duration_ms"] == 1250
    assert manifest["wav"]["duration_ms"] == 8000


def test_confirmation_defaults_to_no_without_writing(tmp_path: Path) -> None:
    replay = _replay_dir(tmp_path)
    original = (replay / "tts" / "a.opus").read_bytes()

    result = slim_audio(SlimAudioSettings.load([str(replay)]), _console(), ask=lambda _: "")

    assert result is None
    assert (replay / "tts" / "a.opus").read_bytes() == original


def test_confirmation_accepts_y(tmp_path: Path) -> None:
    replay = _replay_dir(tmp_path)

    result = slim_audio(SlimAudioSettings.load([str(replay), "--time", "0.5"]), _console(), ask=lambda _: "y")

    assert result is not None
    assert result.files == 2
