"""阿里云 TTS 合成：消费一份不可变 replay，输出 ``replay/tts/*.opus``。

渲染器依赖 ``TTSBackend`` 协议，测试可以注入内存 fake；生产入口只构造阿里云
DashScope 后端，不再暴露会改变 replay 解释方式的 backend / voice / rps 参数。
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import io
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
import soundfile as sf
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from rich.console import Console
from rich.progress import Progress, TaskID

from brawl.paths import REPO_ROOT
from brawl.runtime.files import UnsafePathError, atomic_write, read_regular_bytes
from brawl.runtime.journal import JournalCorruptionError, load_replay_sentences
from brawl.runtime.types import Sentence


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

    model_config = ConfigDict(strict=True, extra="forbid")

    file: str
    backend: str
    duration_ms: int = Field(ge=0)
    voice_id: str
    text_sha256: str
    audio_sha256: str


class Manifest(BaseModel):
    """tts/manifest.json：call_id -> 记录。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    entries: dict[str, ManifestEntry] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        if path.is_symlink():
            raise TTSError(f"manifest.json 不得是软链接：{path}")
        if not path.exists():
            return cls()
        if not path.is_file():
            raise TTSError(f"manifest.json 不是普通文件：{path}")
        try:
            return cls.model_validate_json(read_regular_bytes(path), strict=True)
        except (OSError, UnsafePathError, ValidationError) as exc:
            raise TTSError(f"manifest.json 已损坏：{exc}") from exc

    def save(self, path: Path) -> None:
        try:
            atomic_write(path, self.model_dump_json(indent=2).encode("utf-8"))
        except (OSError, UnsafePathError) as exc:
            raise TTSError(f"无法安全写入 manifest.json：{exc}") from exc


class _TTSSetup(BaseModel):
    """TTS 从玩法 meta 读取的最小、跨玩法投影。"""

    model_config = ConfigDict(strict=True, extra="allow")

    voices: list[str] = Field(min_length=1)
    host_voice: str = Field(min_length=1)

    @model_validator(mode="after")
    def _all_voices_exist(self) -> _TTSSetup:
        if any(not voice.strip() for voice in self.voices):
            raise ValueError("setup.voices 不得包含空音色")
        if not self.host_voice.strip():
            raise ValueError("setup.host_voice 不得为空")
        return self


class _ReplayMeta(BaseModel):
    """后处理只认带版本的 replay；玩法字段由各玩法自己的边界继续收紧。"""

    model_config = ConfigDict(strict=True, extra="allow")

    schema_version: Literal[1]
    game: str = Field(min_length=1)
    player: Literal["random", "opencode"]
    setup: _TTSSetup


# ---------------------------------------------------------------- 后端协议


class TTSBackend(Protocol):
    """TTS 后端：给定 voice_id 和文本，返回 WAV 字节。渲染器负责再封装成 opus。"""

    name: ClassVar[str]
    fingerprint: ClassVar[str]

    async def synthesize(self, text: str, voice_id: str) -> bytes: ...


_DASHSCOPE_AUDIO_HOST = "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"


def dashscope_audio_url(raw: object) -> str:
    """规范化并校验 DashScope 返回的临时 OSS 音频地址。"""
    if not isinstance(raw, str):
        raise TTSError("DashScope 音频下载地址不是字符串")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise TTSError("DashScope 音频下载地址格式非法") from exc
    if parsed.scheme not in {"http", "https"}:
        raise TTSError(f"DashScope 音频下载地址协议不受信任：{parsed.scheme or 'missing'}")
    if parsed.hostname != _DASHSCOPE_AUDIO_HOST:
        raise TTSError(f"DashScope 音频下载地址域名不受信任：{parsed.hostname or 'missing'}")
    if parsed.username is not None or parsed.password is not None or port is not None:
        raise TTSError("DashScope 音频下载地址不得包含用户信息或自定义端口")
    if not parsed.path.startswith("/"):
        raise TTSError("DashScope 音频下载地址缺少绝对路径")
    return urlunsplit(("https", _DASHSCOPE_AUDIO_HOST, parsed.path, parsed.query, ""))


class DashScopeTTSBackend:
    """阿里云百炼 Qwen3-TTS。"""

    name = "dashscope"
    _URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    _MODEL = "qwen3-tts-vd-2026-01-26"
    fingerprint = f"{name}:{_MODEL}"

    def __init__(
        self,
        api_key: str,
        attempts: int = 3,
        *,
        rps: float | None = None,
        api_transport: httpx.AsyncBaseTransport | None = None,
        audio_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise TTSError("DashScope API key 不得为空")
        if attempts <= 0:
            raise ValueError(f"attempts 必须 > 0，收到 {attempts}")
        self._api_key = api_key
        self._attempts = attempts
        self._clock = _LaunchClock(_DASHSCOPE_RPS if rps is None else rps)
        self._api_transport = api_transport
        self._audio_transport = audio_transport

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        # API 与 OSS 下载必须是两个凭证边界：Authorization 只在提交请求这一跳显式携带，
        # 下载 client 从构造起就没有默认鉴权头，避免任何绝对 URL 继承 API key。
        async with (
            httpx.AsyncClient(timeout=60, transport=self._api_transport) as api_client,
            httpx.AsyncClient(
                timeout=60,
                follow_redirects=False,
                transport=self._audio_transport,
            ) as audio_client,
        ):
            for i in range(self._attempts):
                try:
                    await self._clock.wait_next()
                    resp = await api_client.post(
                        self._URL,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json={"model": self._MODEL, "input": {"text": text, "voice": voice_id}},
                    )
                    resp.raise_for_status()
                    try:
                        body = resp.json()
                    except ValueError as exc:
                        raise TTSError("DashScope 合成结果不是合法 JSON") from exc
                    try:
                        audio_url = dashscope_audio_url(body["output"]["audio"]["url"])
                    except (KeyError, TypeError) as exc:
                        raise TTSError("DashScope 合成结果缺少有效音频地址") from exc
                    audio = await audio_client.get(audio_url)
                    audio.raise_for_status()
                    return audio.content
                except TTSError:
                    # 响应结构、下载地址等协议错误不会靠重试自愈，也不该重复消耗配额。
                    raise
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    retryable = status in (408, 429) or status >= 500
                    if not retryable or i == self._attempts - 1:
                        raise TTSError(f"DashScope 请求失败：{exc}") from exc
                    await asyncio.sleep(2**i)
                except httpx.TransportError as exc:
                    if i == self._attempts - 1:
                        raise TTSError(f"DashScope 请求失败：{exc}") from exc
                    await asyncio.sleep(2**i)
        raise AssertionError("unreachable")

# ---------------------------------------------------------------- 渲染器


def _to_opus(wav: bytes) -> tuple[bytes, int]:
    """把 WAV 字节转成 OGG/Opus，返回 (opus 字节, 时长毫秒)。"""
    try:
        data, sample_rate = sf.read(io.BytesIO(wav))
        if sample_rate <= 0:
            raise ValueError(f"非法采样率：{sample_rate}")
        buf = io.BytesIO()
        sf.write(buf, data, sample_rate, format="OGG", subtype="OPUS")
        duration_ms = round(len(data) / sample_rate * 1000)
        return buf.getvalue(), duration_ms
    except (sf.SoundFileError, TypeError, ValueError) as exc:
        raise TTSError(f"TTS 后端返回的 WAV 无法解码或转码：{exc}") from exc


class TTSRenderer:
    """把 sentences 合成为音频，支持增量续跑。"""

    def __init__(self, backend: TTSBackend, replay_dir: Path) -> None:
        self._backend = backend
        self._replay_dir = Path(replay_dir)
        self._tts_dir = self._replay_dir / "tts"
        self._manifest_path = self._tts_dir / "manifest.json"
        self._console = Console()

    async def render(self) -> None:
        if self._replay_dir.is_symlink():
            raise TTSError(f"replay 目录不得是软链接：{self._replay_dir}")
        if not self._replay_dir.is_dir():
            raise TTSError(f"replay 目录不存在或不是目录：{self._replay_dir}")
        if self._tts_dir.is_symlink():
            raise TTSError(f"TTS 输出目录不得是软链接：{self._tts_dir}")
        if self._tts_dir.exists() and not self._tts_dir.is_dir():
            raise TTSError(f"TTS 输出路径不是目录：{self._tts_dir}")
        meta = self._load_meta()
        sentences = self._load_sentences()
        utterances = self._utterances(sentences, meta.setup.voices, meta.setup.host_voice)

        if not utterances:
            self._console.print("[dim]没有需要合成的公开发言。[/dim]")
            return

        try:
            self._tts_dir.mkdir(exist_ok=True)
        except OSError as exc:
            raise TTSError(f"无法创建 TTS 输出目录：{self._tts_dir}：{exc}") from exc
        with _exclusive_lock(self._tts_dir / ".render.lock"):
            manifest = Manifest.load(self._manifest_path)
            todo = [u for u in utterances if not self._is_done(u, manifest)]

            if not todo:
                self._console.print(f"[dim]全部 {len(utterances)} 条已合成，无需补差。[/dim]")
                return

            self._console.print(f"TTS 开始：共 {len(utterances)} 条，待合成 {len(todo)} 条，后端 {self._backend.name}")
            with Progress() as progress:
                task = progress.add_task("[cyan]合成中...", total=len(todo))
                await self._synthesize_all(todo, manifest, progress, task)
        self._console.print(f"完成：{self._tts_dir}")

    def _load_meta(self) -> _ReplayMeta:
        path = self._replay_dir / "meta.json"
        if path.is_symlink():
            raise TTSError(f"meta.json 不得是软链接：{path}")
        try:
            return _ReplayMeta.model_validate_json(read_regular_bytes(path), strict=True)
        except FileNotFoundError as exc:
            raise TTSError(f"replay 目录缺少 meta.json：{self._replay_dir}") from exc
        except (OSError, UnsafePathError, ValidationError) as exc:
            raise TTSError(f"meta.json 不是可用于 TTS 的新 replay 元信息：{exc}") from exc

    def _load_sentences(self) -> list[Sentence]:
        try:
            return load_replay_sentences(self._replay_dir)
        except (OSError, UnsafePathError, JournalCorruptionError) as exc:
            raise TTSError(f"sentences.jsonl 不是合法的新 replay 日志：{exc}") from exc

    def _utterances(self, sentences: list[Sentence], voices: list[str], host_voice: str) -> list[Utterance]:
        out: list[Utterance] = []
        for sent in sentences:
            speaker = sent.speaker
            for delivery in sent.deliveries:
                if not delivery.spoken:
                    continue
                channels = delivery.channels
                # 只给公开广播或 seat-specific 的文本配音；audience（内心戏/上帝视角）不出声
                if "audience" in channels and "broadcast" not in channels and not any(type(c) is int for c in channels):
                    continue
                text = delivery.text
                if not text.strip():
                    continue
                if speaker == "host":
                    voice_id = host_voice
                    speaker_label = "主持人"
                else:
                    if speaker > len(voices):
                        raise TTSError(f"meta.setup.voices 缺少 {speaker} 号席位音色")
                    voice_id = voices[speaker - 1]
                    speaker_label = f"{speaker}号"
                out.append(Utterance(call_id=f"{sent.seq}.{speaker}", speaker=speaker_label, voice_id=voice_id, text=text))
                break  # 一句只取第一条公开文本
        return out

    def _is_done(self, u: Utterance, manifest: Manifest) -> bool:
        record = manifest.entries.get(u.call_id)
        expected_file = f"{u.call_id}.opus"
        if (
            record is None
            or record.backend != self._backend.fingerprint
            or record.file != expected_file
        ):
            return False
        if record.voice_id != u.voice_id or record.text_sha256 != _text_sha256(u.text):
            return False
        output = self._tts_dir / expected_file
        if output.is_symlink():
            raise TTSError(f"TTS 音频不得是软链接：{output}")
        if not output.is_file():
            return False
        try:
            audio = read_regular_bytes(output)
        except FileNotFoundError:
            return False
        except (OSError, UnsafePathError) as exc:
            raise TTSError(f"无法安全校验 TTS 音频 {output}：{exc}") from exc
        return hashlib.sha256(audio).hexdigest() == record.audio_sha256

    async def _synthesize(self, u: Utterance) -> tuple[Utterance, bytes, int]:
        wav = await self._backend.synthesize(u.text, u.voice_id)
        opus, duration_ms = _to_opus(wav)
        return u, opus, duration_ms

    def _store_result(
        self,
        result: tuple[Utterance, bytes, int],
        manifest: Manifest,
        progress: Progress,
        task: TaskID,
    ) -> None:
        u, opus, duration_ms = result
        file = f"{u.call_id}.opus"
        try:
            atomic_write(self._tts_dir / file, opus)
        except (OSError, UnsafePathError) as exc:
            raise TTSError(f"无法安全写入 TTS 音频 {file}：{exc}") from exc
        manifest.entries[u.call_id] = ManifestEntry(
            file=file,
            backend=self._backend.fingerprint,
            duration_ms=duration_ms,
            voice_id=u.voice_id,
            text_sha256=_text_sha256(u.text),
            audio_sha256=hashlib.sha256(opus).hexdigest(),
        )
        manifest.save(self._manifest_path)
        progress.update(task, advance=1, description=f"[cyan]已合成 {u.call_id} {u.speaker}")

    async def _synthesize_all(
        self,
        utterances: list[Utterance],
        manifest: Manifest,
        progress: Progress,
        progress_task: TaskID,
    ) -> None:
        in_flight: set[asyncio.Task[tuple[Utterance, bytes, int]]] = set()
        try:
            for utterance in utterances:
                in_flight.add(asyncio.create_task(self._synthesize(utterance)))
                if len(in_flight) >= _MAX_IN_FLIGHT:
                    done, in_flight = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                    await self._store_completed(done, manifest, progress, progress_task)
            while in_flight:
                done, in_flight = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
                await self._store_completed(done, manifest, progress, progress_task)
        except BaseException:
            for pending in in_flight:
                pending.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            raise

    async def _store_completed(
        self,
        completed: set[asyncio.Task[tuple[Utterance, bytes, int]]],
        manifest: Manifest,
        progress: Progress,
        progress_task: TaskID,
    ) -> None:
        """取尽同批 task 的结果，避免首个失败掩盖其余 task 异常。"""
        results = await asyncio.gather(*completed, return_exceptions=True)
        first_error: BaseException | None = None
        for result in results:
            if isinstance(result, BaseException):
                if first_error is None:
                    first_error = result
                continue
            if first_error is not None:
                continue
            try:
                self._store_result(result, manifest, progress, progress_task)
            except BaseException as exc:
                first_error = exc
        if first_error is not None:
            raise first_error


# ---------------------------------------------------------------- 速率控制


_DASHSCOPE_RPS = 2.8
_MAX_IN_FLIGHT = 8


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _LaunchClock:
    def __init__(self, rps: float) -> None:
        if rps <= 0:
            raise ValueError(f"rps 必须 >0，收到 {rps}")
        self._interval = 1 / rps
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def wait_next(self) -> None:
        import time

        async with self._lock:
            now = time.monotonic()
            if self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = time.monotonic()
            self._next_at = now + self._interval


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """拒绝同一 replay 的并行 TTS 进程，防止 manifest last-writer-wins。"""
    dir_fd = -1
    lock_fd = -1
    try:
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            lock_fd = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise TTSError(f"TTS 锁文件不是普通文件：{path}")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TTSError(f"同一 replay 已有另一个 TTS 任务在运行：{path.parent.parent}") from exc
        except TTSError:
            raise
        except OSError as exc:
            raise TTSError(f"无法安全取得 TTS 任务锁：{path}：{exc}") from exc
        yield
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        if dir_fd >= 0:
            os.close(dir_fd)


def _api_key() -> str:
    env = os.environ.get("DASHSCOPE_API_KEY")
    if env is not None:
        if not env.strip():
            raise TTSError("环境变量 DASHSCOPE_API_KEY 不得为空")
        return env
    dotenv = dotenv_values(REPO_ROOT / ".env")
    key = dotenv.get("DASHSCOPE_API_KEY")
    if not key or not key.strip():
        raise TTSError(".env 或环境变量缺 DASHSCOPE_API_KEY")
    return key


async def _run(replay: Path) -> None:
    backend = DashScopeTTSBackend(_api_key())
    renderer = TTSRenderer(backend, replay)
    await renderer.render()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="brawl tts",
        description="按 replay/meta.json 冻结的阿里云音色增量生成 TTS",
    )
    parser.add_argument("replay", type=Path, help="不可变 replay 目录")
    args = parser.parse_args(argv)
    asyncio.run(_run(args.replay))
