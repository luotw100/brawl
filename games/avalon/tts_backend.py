"""TTS 合成后端：Protocol + AliCloudQwen3TTS。

照搬 core.actor.ModelBackend 的 Protocol 模式：核心逻辑只调用 synthesize()，不关心具体云端实现。
"""

from __future__ import annotations

import asyncio
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
