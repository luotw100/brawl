"""框架层 console / TTS 渲染器测试。"""

from __future__ import annotations

import json
import wave
from io import BytesIO, StringIO

import pytest
from rich.console import Console

from core.sr.console import ConsoleRenderer, InstantTypist
from core.sr.journal import DirJournal
from core.sr.tts import TTSRenderer
from core.sr.types import Delivery, Sentence


class _FakeTTSBackend:
    name = "fake"
    fingerprint = "fake:v1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        self.calls.append((text, voice_id))
        out = BytesIO()
        with wave.open(out, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(b"\0\0" * 240)
        return out.getvalue()


def test_console_renders_public_and_audience_text():
    console = Console(file=StringIO())
    renderer = ConsoleRenderer(typist=InstantTypist(), console=console)
    renderer.render(
        [
            Sentence(
                seq=0,
                commit_id=0,
                turn_id="q1.a1.propose",
                speaker=3,
                deliveries=[
                    Delivery(
                        text="主持人口播同时面向全场和观众。",
                        channels=["broadcast", "audience"],
                    ),
                    Delivery(text="（内心）希望别翻车。", channels=["audience"]),
                ],
            )
        ],
        {"setup": {"roles": ["merlin", "servant", "assassin"]}},
    )
    out = console.file.getvalue()  # type: ignore[attr-defined]
    assert "主持人口播" in out
    assert "📢公开 👁上帝视角" in out
    assert "内心" in out
    assert "3号（assassin）" in out


@pytest.mark.asyncio
async def test_tts_injected_backend_writes_opus_and_manifest(tmp_path):
    replay = tmp_path / "rep"
    journal = DirJournal(replay)
    journal.write_meta({
        "schema_version": 1,
        "game": "test",
        "player": "random",
        "setup": {"voices": ["voice-1", "voice-2"], "host_voice": "host-voice"},
    })
    journal.append_batch([
        Sentence(
            seq=-1,
            turn_id="host.opening",
            speaker="host",
            deliveries=[Delivery(text="开局。", channels=["broadcast"])],
        ),
    ])
    journal.append_batch([
        Sentence(
            seq=-1,
            turn_id="q1.a1.propose",
            speaker=1,
            deliveries=[Delivery(text="上任务。", channels=["broadcast"])],
        ),
    ])

    backend = _FakeTTSBackend()
    renderer = TTSRenderer(backend, replay)
    await renderer.render()

    manifest = json.loads((replay / "tts" / "manifest.json").read_text(encoding="utf-8"))
    assert "entries" in manifest
    assert "0.host" in manifest["entries"]
    assert "1.1" in manifest["entries"]
    assert (replay / "tts" / "0.host.opus").is_file()
    assert (replay / "tts" / "1.1.opus").is_file()
    assert backend.calls == [("开局。", "host-voice"), ("上任务。", "voice-1")]

    # 完整且 fingerprint 相同的二次渲染必须零调用。
    backend.calls.clear()
    await renderer.render()
    assert backend.calls == []

    # 单个音频被截断时只补这一条，不重做健康缓存。
    (replay / "tts" / "1.1.opus").write_bytes(b"")
    await renderer.render()
    assert backend.calls == [("上任务。", "voice-1")]
    assert (replay / "tts" / "1.1.opus").stat().st_size > 0

    # 同名后端切换具体模型 fingerprint 后，旧音频不得被混用。
    class _V2Backend(_FakeTTSBackend):
        fingerprint = "fake:v2"

    upgraded = _V2Backend()
    await TTSRenderer(upgraded, replay).render()
    assert upgraded.calls == [("开局。", "host-voice"), ("上任务。", "voice-1")]
    updated = json.loads((replay / "tts" / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["backend"] for entry in updated["entries"].values()} == {"fake:v2"}
