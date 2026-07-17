"""通用 TTS 合成：消费 sentences.jsonl + meta.json，输出 replay/tts/*.opus。

这是 sentence runtime 的音频回放层。它只看 deliveries 里的公开文本，不看玩法
action；因此任何游戏只要产出 sentences + meta.voices 就能复用。

后端通过 TTSBackend 协议接入，目前自带：
- silence：写静音 opus（测试 / 无 key 时占位）。
- dashscope：阿里云百炼 Qwen3-TTS（需要 games/.env 或环境变量 DASHSCOPE_API_KEY）。
- selfhosted：局域网自托管 Qwen3-TTS（需要 --self-hosted-url）。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

import httpx
import soundfile as sf
from dotenv import dotenv_values
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from rich.console import Console
from rich.progress import Progress, TaskID


class TTSError(Exception):
    """用户可理解的 TTS 配置/运行错误：CLI 只打印消息、不甩 traceback。"""


# ---------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class Utterance:
    """一条待配音的公开发言。"""

    call_id: str
    speaker: str  # "host" 或 "几号"
    voice_id: str
    text: str


class ManifestEntry(BaseModel):
    """已合成的音频记录。"""

    file: str
    backend: str
    duration_ms: int


class Manifest(BaseModel):
    """tts/manifest.json：call_id -> 记录。"""

    entries: dict[str, ManifestEntry] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if not path.is_file():
            return cls()
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 后端协议


class TTSBackend(Protocol):
    """TTS 后端：给定 voice_id 和文本，返回 WAV 字节。渲染器负责再封装成 opus。"""

    name: ClassVar[str]

    async def synthesize(self, text: str, voice_id: str) -> bytes: ...


class SilenceTTSBackend:
    """测试后端：写 1 秒静音 opus。不依赖网络/API key。"""

    name = "silence"

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        import numpy as np

        sr = 24000
        data = np.zeros(sr, dtype=np.float32)
        buf = io.BytesIO()
        sf.write(buf, data, sr, format="OGG", subtype="OPUS")
        return buf.getvalue()


class DashScopeTTSBackend:
    """阿里云百炼 Qwen3-TTS。"""

    name = "dashscope"
    _URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    _MODEL = "qwen3-tts-vd-2026-01-26"

    def __init__(self, api_key: str, attempts: int = 3) -> None:
        self._api_key = api_key
        self._attempts = attempts

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self._api_key}"}, timeout=60
        ) as client:
            for i in range(self._attempts):
                try:
                    resp = await client.post(
                        self._URL,
                        json={"model": self._MODEL, "input": {"text": text, "voice": voice_id}},
                    )
                    body = resp.json()
                    if resp.status_code != 200:
                        raise TTSError(f"DashScope 合成失败（{resp.status_code}）：{body.get('code')} {body.get('message')}")
                    audio = await client.get(body["output"]["audio"]["url"])
                    return audio.content
                except (httpx.HTTPError, TTSError):
                    if i == self._attempts - 1:
                        raise
                    await asyncio.sleep(2**i)
        raise AssertionError("unreachable")


class SelfHostedTTSBackend:
    """局域网自托管 Qwen3-TTS。"""

    name = "selfhosted"

    def __init__(self, base_url: str, attempts: int = 2) -> None:
        self._base_url = base_url.rstrip("/")
        self._attempts = attempts
        self._voice_ids: dict[str, str] = {}

    async def preflight(self, handles: set[str]) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            health = await self._request_json(client, "GET", "/v1/health")
            if not health.get("ok"):
                raise TTSError(f"自托管 TTS 不健康：{health}")
            body = await self._request_json(client, "GET", "/v1/voices")
            voices = body.get("voices", [])
            by_id = {v["voice_id"]: v["voice_id"] for v in voices}
            by_name = {v["name"]: v["voice_id"] for v in voices}
            resolved = {}
            for h in handles:
                if h in by_id:
                    resolved[h] = by_id[h]
                elif h in by_name:
                    resolved[h] = by_name[h]
            missing = sorted(handles - set(resolved))
            if missing:
                raise TTSError(f"自托管 TTS 缺 voice：{'、'.join(missing)}")
            self._voice_ids = resolved

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        voice = self._voice_ids[voice_id]
        async with httpx.AsyncClient(timeout=300) as client:
            for i in range(self._attempts):
                try:
                    resp = await client.post(
                        f"{self._base_url}/v1/audio/speech",
                        json={"voice": voice, "text": text, "language": "Chinese", "format": "wav"},
                    )
                    if resp.status_code != 200:
                        raise TTSError(f"自托管合成失败（{resp.status_code}）：{resp.text[:500]}")
                    return resp.content
                except (httpx.HTTPError, TTSError):
                    if i == self._attempts - 1:
                        raise
                    await asyncio.sleep(2**i)
        raise AssertionError("unreachable")

    async def _request_json(self, client: httpx.AsyncClient, method: str, path: str) -> dict[str, Any]:
        resp = await client.request(method, f"{self._base_url}{path}")
        if resp.status_code != 200:
            raise TTSError(f"自托管服务调用失败（{resp.status_code}）：{resp.text[:500]}")
        return resp.json()


# ---------------------------------------------------------------- 渲染器


def _to_opus(wav: bytes) -> tuple[bytes, int]:
    """把 WAV 字节转成 OGG/Opus，返回 (opus 字节, 时长毫秒)。"""
    data, sr = sf.read(io.BytesIO(wav))
    buf = io.BytesIO()
    sf.write(buf, data, sr, format="OGG", subtype="OPUS")
    duration_ms = round(len(data) / sr * 1000)
    return buf.getvalue(), duration_ms


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class TTSRenderer:
    """把 sentences 合成为音频，支持增量续跑。"""

    def __init__(self, backend: TTSBackend, replay_dir: Path, *, rps: float = 2.8) -> None:
        self._backend = backend
        self._replay_dir = Path(replay_dir)
        self._rps = rps
        self._tts_dir = self._replay_dir / "tts"
        self._manifest_path = self._tts_dir / "manifest.json"
        self._console = Console()

    async def render(self) -> None:
        self._tts_dir.mkdir(exist_ok=True)
        meta = self._load_meta()
        voices, host_voice = self._voices(meta)
        sentences = self._load_sentences()
        utterances = self._utterances(sentences, voices, host_voice)

        if not utterances:
            self._console.print("[dim]没有需要合成的公开发言。[/dim]")
            return

        manifest = Manifest.load(self._manifest_path)
        todo = [u for u in utterances if not self._is_done(u, manifest)]

        if not todo:
            self._console.print(f"[dim]全部 {len(utterances)} 条已合成，无需补差。[/dim]")
            return

        if isinstance(self._backend, SelfHostedTTSBackend):
            await self._backend.preflight({u.voice_id for u in todo})

        self._console.print(f"TTS 开始：共 {len(utterances)} 条，待合成 {len(todo)} 条，后端 {self._backend.name}")
        clock = _LaunchClock(self._rps)
        with Progress() as progress:
            task = progress.add_task("[cyan]合成中...", total=len(todo))
            for u in todo:
                await clock.wait_next()
                await self._synthesize_one(u, manifest, progress, task)
        manifest.save(self._manifest_path)
        self._console.print(f"完成：{self._tts_dir}")

    def _load_meta(self) -> dict[str, Any]:
        path = self._replay_dir / "meta.json"
        if not path.is_file():
            raise TTSError(f"replay 目录缺少 meta.json：{self._replay_dir}")
        return json.loads(path.read_text(encoding="utf-8")).get("setup", {})

    def _load_sentences(self) -> list[dict[str, Any]]:
        path = self._replay_dir / "sentences.jsonl"
        if not path.is_file():
            raise TTSError(f"replay 目录缺少 sentences.jsonl：{self._replay_dir}")
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _voices(self, meta: dict[str, Any]) -> tuple[list[str], str]:
        # 音色 id 按后端取：自托管后端用自托管 voice_id（meta.setup.self_hosted_voices），
        # 云端/静音用云端 voice_id（meta.setup.voices）——两套 id 在生成时都已快照进 meta。
        self_hosted = self._backend.name == "selfhosted"
        voices = meta.get("self_hosted_voices") if self_hosted else meta.get("voices")
        host_voice = meta.get("host_self_hosted_voice") if self_hosted else meta.get("host_voice")
        if voices is None or host_voice is None:
            which = "self_hosted_voices / host_self_hosted_voice" if self_hosted else "voices / host_voice"
            raise TTSError(f"meta.json setup 缺 {which}，无法配音")
        return voices, host_voice

    def _utterances(self, sentences: list[dict[str, Any]], voices: list[str], host_voice: str) -> list[Utterance]:
        out: list[Utterance] = []
        for sent in sentences:
            speaker = sent.get("speaker")
            for d in sent.get("deliveries", []):
                chs = d.get("channels") or []
                # 只给公开广播或 seat-specific 的文本配音；audience（内心戏/上帝视角）不出声
                if "audience" in chs and "broadcast" not in chs and not any(isinstance(c, int) for c in chs):
                    continue
                text = d.get("text", "")
                if not text.strip():
                    continue
                if speaker == "host":
                    voice_id = host_voice
                    speaker_label = "主持人"
                elif isinstance(speaker, int):
                    if not 1 <= speaker <= len(voices):
                        continue
                    voice_id = voices[speaker - 1]
                    speaker_label = f"{speaker}号"
                else:
                    continue
                out.append(Utterance(call_id=f"{sent['seq']}.{speaker}", speaker=speaker_label, voice_id=voice_id, text=text))
                break  # 一句只取第一条公开文本
        return out

    def _is_done(self, u: Utterance, manifest: Manifest) -> bool:
        record = manifest.entries.get(u.call_id)
        if record is None or record.backend != self._backend.name:
            return False
        return (self._tts_dir / record.file).is_file()

    async def _synthesize_one(self, u: Utterance, manifest: Manifest, progress: Progress, task: TaskID) -> None:
        wav = await self._backend.synthesize(u.text, u.voice_id)
        opus, duration_ms = _to_opus(wav)
        file = f"{u.call_id}.opus"
        _atomic_write(self._tts_dir / file, opus)
        manifest.entries[u.call_id] = ManifestEntry(file=file, backend=self._backend.name, duration_ms=duration_ms)
        manifest.save(self._manifest_path)
        progress.update(task, advance=1, description=f"[cyan]已合成 {u.call_id} {u.speaker}")


# ---------------------------------------------------------------- 速率控制


class _LaunchClock:
    def __init__(self, rps: float) -> None:
        if rps <= 0:
            raise ValueError(f"rps 必须 >0，收到 {rps}")
        self._interval = 1 / rps
        self._next_at = 0.0

    async def wait_next(self) -> None:
        import time

        now = time.monotonic()
        if self._next_at > now:
            await asyncio.sleep(self._next_at - now)
            now = time.monotonic()
        self._next_at = now + self._interval


# ---------------------------------------------------------------- CLI


class TTSSettings(BaseSettings):
    """brawl tts 的 CLI 参数面。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_TTS_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    dir: CliPositionalArg[Path]
    backend: Literal["silence", "dashscope", "selfhosted"] = "silence"
    rps: float = 2.8
    self_hosted_url: str = "http://127.0.0.1:8010"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            CliSettingsSource(
                settings_cls,
                cli_parse_args=cls._cli_args if cls._cli_args is not None else [],
                cli_prog_name="brawl tts",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> TTSSettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None


def _api_key() -> str:
    env = os.environ.get("DASHSCOPE_API_KEY")
    if env:
        return env
    dotenv = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    key = dotenv.get("DASHSCOPE_API_KEY")
    if not key:
        raise TTSError("games/.env 或环境变量缺 DASHSCOPE_API_KEY")
    return key


def _backend(name: str, settings: TTSSettings) -> TTSBackend:
    if name == "silence":
        return SilenceTTSBackend()
    if name == "dashscope":
        return DashScopeTTSBackend(_api_key())
    if name == "selfhosted":
        return SelfHostedTTSBackend(settings.self_hosted_url)
    raise TTSError(f"未知 TTS 后端：{name}")


async def _run(settings: TTSSettings) -> None:
    backend = _backend(settings.backend, settings)
    renderer = TTSRenderer(backend, settings.dir, rps=settings.rps)
    await renderer.render()


def main(argv: list[str]) -> None:
    settings = TTSSettings.load(argv)
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main([])
