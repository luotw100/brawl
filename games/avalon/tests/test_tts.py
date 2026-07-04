"""avalon-tts：CLI 参数与 DashScope 请求节流。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from avalon import tts
from avalon.tts import LaunchClock, TtsDashboard, TtsJob, TtsSettings, Utterance
from avalon.tts_backend import SelfHostedQwen3TTS


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


def test_backend_defaults_to_cloud(tmp_path: Path) -> None:
    settings = TtsSettings.load([str(_replay_dir(tmp_path))])
    assert settings.backend == "qwen3-tts-cloud"


def test_backend_accepts_self_hosted(tmp_path: Path) -> None:
    settings = TtsSettings.load([str(_replay_dir(tmp_path)), "--backend", "qwen3-tts-self-hosted"])
    assert settings.backend == "qwen3-tts-self-hosted"


def test_backend_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        TtsSettings.load([str(_replay_dir(tmp_path)), "--backend", "garbage"])


def test_self_hosted_voice_snapshot_falls_back_to_cast(tmp_path: Path) -> None:
    settings = TtsSettings.load([str(_replay_dir(tmp_path)), "--backend", "qwen3-tts-self-hosted"])

    voices, host_voice = tts._voices(settings, {"cast": ["fox", "diva"], "host_cast": "herald"})

    assert voices == ["fox", "diva"]
    assert host_voice == "herald"


async def test_self_hosted_backend_resolves_voice_names(tmp_path: Path) -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"ok": True, "device": "cuda:0", "base_loaded": True, "vd_loaded": False, "voices": 1})
        if request.url.path == "/v1/voices":
            return httpx.Response(
                200,
                json={"voices": [{"voice_id": "local-vd-fox-1", "name": "fox", "language": "Chinese", "created_at": "now", "preview_audio_url": "/v1/voices/local-vd-fox-1/preview.wav"}]},
            )
        if request.url.path == "/v1/audio/speech":
            calls.append(json.loads(request.content))
            return httpx.Response(200, content=b"RIFF")
        return httpx.Response(404, json={"detail": "missing"})

    replay = _replay_dir(tmp_path)
    settings = TtsSettings.load([str(replay), "--backend", "qwen3-tts-self-hosted"])
    utterance = Utterance(call_id="c1", speaker="1号", persona="fox", voice_id="fox", text="可以。")
    dashboard = TtsDashboard(settings, utterances=1, done=0, jobs=[TtsJob(1, utterance)], tts_dir=replay / "tts", console=tts.Console(), backend_label="SelfHostedQwen3TTS")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        backend = SelfHostedQwen3TTS(client, "http://tts.local")
        await backend.preflight({"fox"})
        wav = await backend.synthesize(dashboard, utterance)

    assert wav == b"RIFF"
    assert calls == [{"voice": "local-vd-fox-1", "text": "可以。", "language": "Chinese", "format": "wav"}]
