"""TTS 的 replay 边界、并发发射与只读 CLI。"""

from __future__ import annotations

import asyncio
import json
import wave
from io import BytesIO

import pytest

import brawl.runtime.tts as tts_mod
from brawl.runtime.journal import DirJournal
from brawl.runtime.tts import TTSError, TTSRenderer
from brawl.runtime.types import Delivery, Sentence


def _wav() -> bytes:
    out = BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(b"\0\0" * 240)
    return out.getvalue()


class _OverlapBackend:
    name = "fake"
    fingerprint = "fake:v1"

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return _wav()


class _InvalidAudioBackend:
    name = "fake"
    fingerprint = "fake:v1"

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        return b"not-a-wav"


class _FailingBackend:
    name = "fake"
    fingerprint = "fake:v1"

    def __init__(self) -> None:
        self.active = 0

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        self.active += 1
        try:
            if text == "第0句":
                await asyncio.sleep(0.01)
                raise TTSError("boom")
            await asyncio.sleep(10)
            return _wav()
        finally:
            self.active -= 1


class _AllFailBackend:
    name = "fake"
    fingerprint = "fake:v1"

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        await asyncio.sleep(0.01)
        raise TTSError(f"failed: {text}")


def _meta() -> dict:
    return {
        "schema_version": 1,
        "game": "test",
        "backend": "random",
        "setup": {"voices": ["voice-1"], "host_voice": "host-voice"},
    }


@pytest.mark.asyncio
async def test_malformed_sentence_never_becomes_output_path(tmp_path) -> None:
    replay = tmp_path / "rep"
    replay.mkdir()
    (replay / "meta.json").write_text(json.dumps(_meta()), encoding="utf-8")
    malformed = {
        "seq": "../outside",
        "commit_id": 0,
        "turn_id": "bad",
        "speaker": "host",
        "deliveries": [{"text": "bad", "channels": ["broadcast"]}],
    }
    (replay / "sentences.jsonl").write_text(json.dumps(malformed) + "\n", encoding="utf-8")

    with pytest.raises(TTSError, match="不是合法的新 replay 日志"):
        await TTSRenderer(_OverlapBackend(), replay).render()

    assert not (tmp_path / "outside.host.opus").exists()
    assert not (replay / "tts").exists()


@pytest.mark.asyncio
async def test_renderer_bounds_requests_but_keeps_them_concurrent(tmp_path) -> None:
    replay = tmp_path / "rep"
    journal = DirJournal(replay)
    journal.write_meta(_meta())
    for index in range(12):
        journal.append_batch([
            Sentence(
                seq=-1,
                turn_id=f"turn.{index}",
                speaker=1,
                deliveries=[Delivery(text=f"第{index}句", channels=["broadcast"])],
            )
        ])

    backend = _OverlapBackend()
    await TTSRenderer(backend, replay).render()

    assert 1 < backend.max_active <= tts_mod._MAX_IN_FLIGHT


@pytest.mark.asyncio
async def test_invalid_backend_audio_is_a_tts_error_without_manifest(tmp_path) -> None:
    replay = tmp_path / "rep"
    journal = DirJournal(replay)
    journal.write_meta(_meta())
    journal.append_batch([
        Sentence(
            seq=-1,
            turn_id="turn.0",
            speaker=1,
            deliveries=[Delivery(text="发言", channels=["broadcast"])],
        )
    ])

    with pytest.raises(TTSError, match="WAV 无法解码或转码"):
        await TTSRenderer(_InvalidAudioBackend(), replay).render()

    assert not (replay / "tts" / "manifest.json").exists()
    assert not list((replay / "tts").glob("*.opus"))


@pytest.mark.asyncio
async def test_failed_synthesis_cancels_sibling_requests(tmp_path) -> None:
    replay = tmp_path / "rep"
    journal = DirJournal(replay)
    journal.write_meta(_meta())
    for index in range(4):
        journal.append_batch([
            Sentence(
                seq=-1,
                turn_id=f"turn.{index}",
                speaker=1,
                deliveries=[Delivery(text=f"第{index}句", channels=["broadcast"])],
            )
        ])

    backend = _FailingBackend()
    with pytest.raises(TTSError, match="boom"):
        await TTSRenderer(backend, replay).render()

    assert backend.active == 0


@pytest.mark.asyncio
async def test_multiple_failed_tasks_are_all_retrieved(tmp_path) -> None:
    replay = tmp_path / "rep"
    journal = DirJournal(replay)
    journal.write_meta(_meta())
    for index in range(tts_mod._MAX_IN_FLIGHT):
        journal.append_batch([
            Sentence(
                seq=-1,
                turn_id=f"turn.{index}",
                speaker=1,
                deliveries=[Delivery(text=f"第{index}句", channels=["broadcast"])],
            )
        ])

    loop = asyncio.get_running_loop()
    unhandled: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        with pytest.raises(TTSError, match="failed"):
            await TTSRenderer(_AllFailBackend(), replay).render()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert unhandled == []


def test_tts_lock_rejects_parallel_renderer_and_symlink(tmp_path) -> None:
    tts_dir = tmp_path / "tts"
    tts_dir.mkdir()
    lock_path = tts_dir / ".render.lock"
    with (
        tts_mod._exclusive_lock(lock_path),
        pytest.raises(TTSError, match="另一个 TTS"),
        tts_mod._exclusive_lock(lock_path),
    ):
        pass

    lock_path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("", encoding="utf-8")
    lock_path.symlink_to(outside)
    with (
        pytest.raises(TTSError, match="安全取得 TTS 任务锁"),
        tts_mod._exclusive_lock(lock_path),
    ):
        pass


def test_tts_cli_rejects_backend_and_rate_overrides(tmp_path) -> None:
    with pytest.raises(SystemExit):
        tts_mod.main([str(tmp_path), "--backend", "silence"])
    with pytest.raises(SystemExit):
        tts_mod.main([str(tmp_path), "--rps", "99"])
