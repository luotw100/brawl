"""框架层 console / TTS 渲染器测试。"""

from __future__ import annotations

import json
from io import StringIO

import pytest
from rich.console import Console

from core.sr.console import ConsoleRenderer, InstantTypist
from core.sr.tts import SilenceTTSBackend, TTSRenderer


def test_console_renders_public_and_audience_text():
    console = Console(file=StringIO())
    renderer = ConsoleRenderer(typist=InstantTypist(), console=console)
    renderer.render(
        [
            {
                "seq": 0,
                "turn_id": "q1.a1.propose",
                "speaker": 3,
                "raw": "",
                "deliveries": [
                    {"text": "我提议 1、2 上任务。", "channels": ["broadcast"]},
                    {"text": "（内心）希望别翻车。", "channels": ["audience"]},
                ],
            }
        ],
        {"setup": {"roles": ["merlin", "servant", "assassin"]}},
    )
    out = console.file.getvalue()  # type: ignore[attr-defined]
    assert "我提议" in out
    assert "内心" in out
    assert "3号（assassin）" in out


@pytest.mark.asyncio
async def test_tts_silence_backend_writes_opus_and_manifest(tmp_path):
    replay = tmp_path / "rep"
    replay.mkdir()
    meta = {"setup": {"voices": ["voice-1", "voice-2"], "host_voice": "host-voice"}}
    (replay / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    sentences = [
        {"seq": 0, "turn_id": "host.opening", "speaker": "host", "deliveries": [{"text": "开局。", "channels": ["broadcast"]}]},
        {"seq": 1, "turn_id": "q1.a1.propose", "speaker": 1, "deliveries": [{"text": "上任务。", "channels": ["broadcast"]}]},
    ]
    (replay / "sentences.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in sentences), encoding="utf-8"
    )

    renderer = TTSRenderer(SilenceTTSBackend(), replay, rps=1000)
    await renderer.render()

    manifest = json.loads((replay / "tts" / "manifest.json").read_text(encoding="utf-8"))
    assert "entries" in manifest
    assert "0.host" in manifest["entries"]
    assert "1.1" in manifest["entries"]
    assert (replay / "tts" / "0.host.opus").is_file()
    assert (replay / "tts" / "1.1.opus").is_file()
