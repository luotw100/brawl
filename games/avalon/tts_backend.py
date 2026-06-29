"""TTS 合成后端：Protocol + 云端/本地 Qwen3-TTS。

照搬 core.actor.ModelBackend 的 Protocol 模式：核心逻辑只调用 synthesize()，不关心具体实现。
"""

from __future__ import annotations

import asyncio
import io
from typing import Literal, Protocol

import httpx

from avalon.tts import SettingsError, TtsDashboard
from core import qwen3_tts

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


class LocalQwen3TTS:
    """本地 Qwen3-TTS VoiceDesign：text + voice_design prompt → WAV bytes。"""

    def __init__(
        self,
        *,
        vd_model: str,
        device: Literal["auto", "cpu", "cuda"],
        threads: int,
    ) -> None:
        self._torch, self._sf, model_cls = qwen3_tts.import_runtime(SettingsError)
        self._device_map, dtype = qwen3_tts.resolve_device(self._torch, device, threads, SettingsError)
        self._model = qwen3_tts.load_model(model_cls, vd_model, self._device_map, dtype)

    async def synthesize(self, dashboard: TtsDashboard, u: object) -> bytes:
        dashboard.status(u, "Request", "Qwen3 local", 1)
        with self._torch.inference_mode():
            wavs, sr = self._model.generate_voice_design(
                text=u.text,
                language="Chinese",
                instruct=u.voice_id,
                do_sample=False,
                subtalker_dosample=False,
            )
        buf = io.BytesIO()
        self._sf.write(buf, wavs[0], sr, format="WAV")
        return buf.getvalue()
