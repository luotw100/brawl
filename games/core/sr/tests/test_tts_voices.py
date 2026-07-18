"""TTS 音色 id 按后端选择：自托管后端读自托管 voice_id，云端/静音读云端 voice_id。

回归：selfhosted 此前一律读云端 voices（qwen-tts-vd-*），打到自托管 server 上全错。
"""

from __future__ import annotations

from pathlib import Path

from core.sr.tts import SelfHostedTTSBackend, SilenceTTSBackend, TTSRenderer


def _meta() -> dict:
    return {
        "voices": ["cloud-v1", "cloud-v2"],
        "host_voice": "cloud-host",
        "self_hosted_voices": ["local-v1", "local-v2"],
        "host_self_hosted_voice": "local-host",
    }


def test_selfhosted_backend_uses_local_voice_ids(tmp_path: Path) -> None:
    r = TTSRenderer(SelfHostedTTSBackend("http://127.0.0.1:9"), tmp_path)
    voices, host = r._voices(_meta())
    assert voices == ["local-v1", "local-v2"] and host == "local-host"


def test_cloud_backends_use_cloud_voice_ids(tmp_path: Path) -> None:
    r = TTSRenderer(SilenceTTSBackend(), tmp_path)
    voices, host = r._voices(_meta())
    assert voices == ["cloud-v1", "cloud-v2"] and host == "cloud-host"


def test_nonspoken_delivery_stays_in_log_but_is_skipped_by_tts(tmp_path: Path) -> None:
    r = TTSRenderer(SilenceTTSBackend(), tmp_path)
    utterances = r._utterances(
        [{
            "seq": 3,
            "speaker": "host",
            "deliveries": [
                {"text": "【符号账本】1号✔", "channels": ["broadcast"], "spoken": False},
                {"text": "口语播报结果", "channels": ["broadcast"], "spoken": True},
            ],
        }],
        ["player"],
        "host",
    )
    assert len(utterances) == 1
    assert utterances[0].text == "口语播报结果"
