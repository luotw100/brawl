"""TTS 合成后端：Protocol + AliCloudQwen3TTS + LocalEdgeTTS。

照搬 core.actor.ModelBackend 的 Protocol 模式：核心逻辑只调用 synthesize()，不关心后端是云还是本地。
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from avalon.tts import SettingsError, TtsDashboard

_SYNTH_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
_VD_MODEL = "qwen3-tts-vd-2026-01-26"


class TtsSynthesizer(Protocol):
    """一次 TTS 合成的抽象：给定发言和展示板，返回 WAV 字节。"""

    async def synthesize(self, dashboard: TtsDashboard, u: object) -> bytes: ...


class AliCloudQwen3TTS:
    """阿里云百炼千问 3 TTS（DashScope）。"""

    def __init__(self, client: httpx.AsyncClient, attempts: int = 3) -> None:
        self._client = client
        self._attempts = attempts

    async def synthesize(self, dashboard: TtsDashboard, u: object) -> bytes:
        for i in range(self._attempts):
            try:
                attempt = i + 1
                dashboard.status(u, "Request", "DashScope", attempt)
                resp = await self._client.post(_SYNTH_URL, json={"model": _VD_MODEL, "input": {"text": u.text, "voice": u.voice_id}})
                body = resp.json()
                if resp.status_code != 200:
                    raise SettingsError(f"合成失败（{resp.status_code}）：{body.get('code')} {body.get('message')}")
                dashboard.status(u, "Download", "audio", attempt)
                audio = await self._client.get(body["output"]["audio"]["url"])
                return audio.content
            except (httpx.HTTPError, SettingsError):
                if i == self._attempts - 1:
                    raise
                dashboard.status(u, "Retry", f"sleep {2**i}s", i + 1)
                await asyncio.sleep(2**i)
        raise AssertionError("unreachable")


class LocalEdgeTTS:
    """本地 Edge TTS：用微软免费语音合成（edge-tts），无需 API key。"""

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural") -> None:
        try:
            import edge_tts  # noqa: F401
        except ImportError as err:
            raise SettingsError("edge-tts 未安装，请执行：pip install edge-tts") from err
        self._voice = voice

    async def synthesize(self, dashboard: TtsDashboard, u: object) -> bytes:
        from edge_tts import Communicate

        dashboard.status(u, "Request", "EdgeTTS", 1)
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3_tmp:
            mp3_path = Path(mp3_tmp.name)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wav_tmp:
            wav_path = Path(wav_tmp.name)
        try:
            await Communicate(u.text, self._voice).save(str(mp3_path))
            dashboard.status(u, "Download", "mp3→wav", 1)
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp3_path), "-f", "wav", str(wav_path)],
                check=True, capture_output=True,
            )
            return wav_path.read_bytes()
        finally:
            mp3_path.unlink(missing_ok=True)
            wav_path.unlink(missing_ok=True)
