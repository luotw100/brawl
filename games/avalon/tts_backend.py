"""TTS 合成后端：Protocol + 云端/自托管 Qwen3-TTS。

照搬 core.actor.ModelBackend 的 Protocol 模式：核心逻辑只调用 synthesize()，不关心具体实现。
"""

from __future__ import annotations

import asyncio
import time
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


class SelfHostedQwen3TTS:
    """局域网自托管 Qwen3-TTS：voice_id/name + text → WAV bytes。"""

    def __init__(self, client: httpx.AsyncClient, base_url: str, attempts: int = 2) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._attempts = attempts
        self._voice_ids: dict[str, str] = {}

    async def preflight(self, handles: set[str]) -> None:
        health = await self._json("GET", "/v1/health")
        if not health.get("ok"):
            raise SettingsError(f"自托管 Qwen3-TTS 不健康：{health}")

        body = await self._json("GET", "/v1/voices")
        voices = body.get("voices", [])
        by_id = {v["voice_id"]: v["voice_id"] for v in voices}
        by_name = {v["name"]: v["voice_id"] for v in voices}
        resolved = {h: by_id[h] if h in by_id else by_name[h] for h in handles if h in by_id or h in by_name}
        missing = sorted(handles - set(resolved))
        if missing:
            raise SettingsError(f"自托管 Qwen3-TTS 缺 voice：{'、'.join(missing)}")
        self._voice_ids = resolved

    async def synthesize(self, dashboard: TtsDashboard, u: object) -> bytes:
        voice = self._voice_ids[u.voice_id]
        for i in range(self._attempts):
            attempt = i + 1
            try:
                started = time.monotonic()
                dashboard.status(u, "Request", voice, attempt)
                resp = await self._client.post(
                    f"{self._base_url}/v1/audio/speech",
                    json={"voice": voice, "text": u.text, "language": "Chinese", "format": "wav"},
                )
                if resp.status_code != 200:
                    raise SettingsError(f"自托管合成失败（{resp.status_code}）：{_error_text(resp)}")
                elapsed = time.monotonic() - started
                dashboard.status(u, "Download", f"{len(resp.content) // 1024}KB {elapsed:.1f}s", attempt)
                return resp.content
            except (httpx.HTTPError, SettingsError):
                if i == self._attempts - 1:
                    raise
                dashboard.status(u, "Retry", f"sleep {2**i}s", attempt)
                await asyncio.sleep(2**i)
        raise AssertionError("unreachable")

    async def _json(self, method: str, path: str) -> dict:
        resp = await self._client.request(method, f"{self._base_url}{path}")
        if resp.status_code != 200:
            raise SettingsError(f"自托管服务调用失败（{resp.status_code}）：{_error_text(resp)}")
        return resp.json()


def _error_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json())
    except ValueError:
        return resp.text[:500]
