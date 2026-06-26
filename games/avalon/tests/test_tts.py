"""avalon-tts：CLI 参数与 DashScope 请求节流。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from avalon import tts
from avalon.tts import LaunchClock, TtsSettings


def _replay_dir(tmp_path: Path) -> Path:
    d = tmp_path / "replay"
    d.mkdir()
    (d / "events.jsonl").write_text("", encoding="utf-8")
    (d / "meta.json").write_text('{"setup": {}}', encoding="utf-8")
    return d


def test_tts_rps_defaults_to_safe_dashscope_rate(tmp_path: Path) -> None:
    settings = TtsSettings.load([str(_replay_dir(tmp_path))])
    assert settings.rps == 2.8


def test_tts_rps_accepts_float_cli_value(tmp_path: Path) -> None:
    settings = TtsSettings.load([str(_replay_dir(tmp_path)), "--rps", "1.5"])
    assert settings.rps == 1.5


@pytest.mark.parametrize("value", ["0", "-1"])
def test_tts_rps_must_be_positive(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValidationError):
        TtsSettings.load([str(_replay_dir(tmp_path)), "--rps", value])


async def test_launch_clock_sends_at_requested_rps(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        nonlocal clock
        sleeps.append(seconds)
        clock += seconds

    monkeypatch.setattr(tts.time, "monotonic", monotonic)
    monkeypatch.setattr(tts.asyncio, "sleep", sleep)

    clocker = LaunchClock(rps=2)
    await clocker.wait_next()
    await clocker.wait_next()
    await clocker.wait_next()

    assert sleeps == [0.5, 0.5]
