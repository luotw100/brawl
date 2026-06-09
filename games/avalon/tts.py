"""`brawl avalon-tts <replay_dir>`：把一局 replay 的公开发言合成成音频。

消费端，像 avalon-replay 一样只读快照、零对局逻辑。音色**全部从 meta.json 查**
（setup.voices / setup.host_voice，开局时从 cast.yaml 快照进来）——本模块不读 cast.yaml，
故 cast.yaml 之后再改也不影响老 replay 的配音。

柔性续合：产物写进 <replay_dir>/tts/，manifest.json 以 call_id 为键记录每条已合成发言。
重复运行只补差集（events 里有、manifest 里没有或音频文件已丢的那些），已合成的跳过。
每条合成成功即刻刷新 manifest 落盘，跑到一半中断也不丢已完成的部分。

只合成"说出口的公开台词"：host/propose/discuss/lady/assassination/banter 的发言字段；
monologue（内心戏）与纯数据事件（vote/quest_result）不出声。暂不传 instructions（语气）。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import httpx
import soundfile as sf
from dotenv import dotenv_values
from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

# 北京地域。与 voice-forge 同一合成接口；这里按"不抽公共函数"自带一份。
_SYNTH_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
_VD_MODEL = "qwen3-tts-vd-2026-01-26"


class SettingsError(Exception):
    """面向用户的错误（目录不对/缺音色/凭证缺失）：cli 入口只打印消息、不甩 traceback。"""


class TtsSettings(BaseSettings):
    """`brawl avalon-tts` 的参数面（settings 即 CLI，flag 自动派生）。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_AVALON_TTS_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    dir: CliPositionalArg[Path]  # replay 目录（含 events.jsonl / meta.json）
    concurrency: int = 4  # 并行合成上限（信号量限流，撞 API 限流时调小）

    @model_validator(mode="after")
    def _check(self) -> TtsSettings:
        if not self.dir.is_dir():
            raise ValueError(f"replay 目录不存在：{self.dir}")
        for name in ("events.jsonl", "meta.json"):
            if not (self.dir / name).is_file():
                raise ValueError(f"{self.dir} 不是 replay 目录（缺 {name}）")
        if self.concurrency < 1:
            raise ValueError(f"concurrency 必须 ≥1，收到 {self.concurrency}")
        return self

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
                cli_prog_name="brawl avalon-tts",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> TtsSettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None


# ---------------------------------------------------------------- 发言枚举


@dataclass(frozen=True)
class Utterance:
    """一条要配音的公开发言。call_id 整局唯一，既是音频文件名也是 manifest 的键。"""

    call_id: str
    speaker: str  # 显示用："3号" / "主持人"
    persona: str  # 人设 id（debug/溯源用）
    voice_id: str  # TTS 音色 id（从 meta 查）
    text: str


def _utterances(events: list[dict[str, Any]], voices: list[str], host_voice: str, cast: list[str], host_cast: str) -> list[Utterance]:
    """从事件流摘出所有公开发言。voices[i]/cast[i] 对应 i+1 号座位（座位号 1 起）。"""

    def seat(no: int, call_id: str, text: str) -> Utterance:
        return Utterance(call_id=call_id, speaker=f"{no}号", persona=cast[no - 1], voice_id=voices[no - 1], text=text)

    out: list[Utterance] = []
    for ev in events:
        t = ev["type"]
        if t == "host":
            out.append(Utterance(call_id=ev["call_id"], speaker="主持人", persona=host_cast, voice_id=host_voice, text=ev["text"]))
        elif t == "propose":
            out.append(seat(ev["leader_no"], ev["call_id"], ev["speech"]))
        elif t == "discuss":
            out.append(seat(ev["seat_no"], ev["call_id"], ev["text"]))
        elif t == "lady":
            out.append(seat(ev["holder_no"], ev["announce_call_id"], ev["speech"]))
        elif t == "assassination":
            out.append(seat(ev["assassin_no"], ev["call_id"], ev["speech"]))
        elif t == "banter":
            out.append(seat(ev["seat_no"], ev["call_id"], ev["text"]))
    return [u for u in out if u.text.strip()]


# ---------------------------------------------------------------- 合成


def _api_key() -> str:
    """边界校验：开工前确认凭证就绪。真实环境变量优先，其次 games/.env。"""
    key = os.environ.get("DASHSCOPE_API_KEY") or dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("DASHSCOPE_API_KEY")
    if not key:
        raise SettingsError("games/.env 缺 DASHSCOPE_API_KEY（阿里云百炼 API key）")
    return key


async def _synthesize(client: httpx.AsyncClient, voice_id: str, text: str, attempts: int = 3) -> bytes:
    """合成一句并下载音频字节。瞬时失败重试几次（指数退避），仍不行则抛。"""
    for i in range(attempts):
        try:
            resp = await client.post(_SYNTH_URL, json={"model": _VD_MODEL, "input": {"text": text, "voice": voice_id}})
            body = resp.json()
            if resp.status_code != 200:
                raise SettingsError(f"合成失败（{resp.status_code}）：{body.get('code')} {body.get('message')}")
            audio = await client.get(body["output"]["audio"]["url"])
            return audio.content
        except (httpx.HTTPError, SettingsError):
            if i == attempts - 1:
                raise
            await asyncio.sleep(2**i)
    raise AssertionError("unreachable")


def _to_opus(wav: bytes) -> tuple[bytes, int]:
    """把合成返回的 wav 字节本地转成 opus（OGG/Opus），并返回 (opus 字节, 真实时长毫秒)。

    在内存里"重写容器"一举三得：①体积约 1/11（语音专用编码，stage 拉取耗时随之骤降，
    尾部不再被截）；②header 由我们写、size 字段诚实——根治百炼流式 wav 那个把 data 块
    大小写成 ~2GB 占位、害得读头的库（wave/mutagen/tinytag）算出十几小时的坑；③时长直接
    取自 soundfile 真解码的采样数（识破假声明），无需再事后读文件。"""
    data, sr = sf.read(io.BytesIO(wav))
    buf = io.BytesIO()
    sf.write(buf, data, sr, format="OGG", subtype="OPUS")
    return buf.getvalue(), round(len(data) / sr * 1000)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """先写同目录临时文件再 os.replace 原子改名：中途被杀也不会留半截文件，读者要么
    看到旧内容要么看到新内容，绝不撞见写到一半的。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 驱动


def _progress() -> Progress:
    """原地刷新的进度条（不滚屏）：当前合成谁 + 已完成/总数 + 耗时。"""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    )


def _read_events(path: Path) -> list[dict[str, Any]]:
    """读 events.jsonl，容忍最后一行半写。

    支持"边跑边合成"：对局还在往 events.jsonl 追加时，最后一行可能只写了一半（换行符
    最后才落）。最后一个非空行解析失败属正常（下次再合即可），跳过；中间行解析失败才是
    真损坏，照常抛。"""
    lines = [s for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]
    events: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break  # 末行半写：对局正在追加，本轮先合成已完整的部分
            raise
    return events


async def _run(settings: TtsSettings, console: Console) -> None:
    meta = json.loads((settings.dir / "meta.json").read_text(encoding="utf-8"))["setup"]
    events = _read_events(settings.dir / "events.jsonl")

    voices, host_voice = meta.get("voices"), meta.get("host_voice")
    if voices is None or host_voice is None:
        raise SettingsError(f"{settings.dir}/meta.json 不自包含音色（缺 setup.voices/host_voice）：用新版引擎重生成或手工补全")

    utterances = _utterances(events, voices, host_voice, meta["cast"], meta["host_cast"])

    # 边界前置校验：任一说话人没铸音色就别开工，免得合成到一半才炸。
    missing = sorted({f"{u.speaker}（{u.persona}）" for u in utterances if not u.voice_id})
    if missing:
        raise SettingsError(f"以下说话人 meta 里无音色 id，无法合成：{'、'.join(missing)}")

    tts_dir = settings.dir / "tts"
    tts_dir.mkdir(exist_ok=True)
    manifest_path = tts_dir / "manifest.json"
    manifest: dict[str, dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}

    # 差集：manifest 里有记录且音频文件还在的，算已完成，跳过。
    todo = [u for u in utterances if not (u.call_id in manifest and (tts_dir / f"{u.call_id}.opus").is_file())]
    done = len(utterances) - len(todo)
    console.print(f"共 {len(utterances)} 条公开发言，已完成 {done} 条，待合成 {len(todo)} 条。")
    if not todo:
        return

    sem = asyncio.Semaphore(settings.concurrency)

    with _progress() as progress:
        task = progress.add_task("准备中", total=len(todo))
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {_api_key()}"}, timeout=60) as client:

            async def one(u: Utterance) -> tuple[Utterance, int]:
                async with sem:
                    progress.update(task, description=f"合成 {u.speaker} · {u.persona}")
                    wav = await _synthesize(client, u.voice_id, u.text)
                    opus, duration_ms = _to_opus(wav)
                    _atomic_write_bytes(tts_dir / f"{u.call_id}.opus", opus)
                    return u, duration_ms

            for coro in asyncio.as_completed([one(u) for u in todo]):
                u, duration_ms = await coro
                manifest[u.call_id] = {
                    "speaker": u.speaker,
                    "persona": u.persona,
                    "voice_id": u.voice_id,
                    "text": u.text,
                    "file": f"{u.call_id}.opus",
                    "duration_ms": duration_ms,
                }
                # 每条原子刷 manifest：中断也不丢已完成的部分，且绝不留半截 manifest。
                _atomic_write_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
                progress.advance(task)

    console.print(f"完成：音频与 manifest.json 已写入 {tts_dir}")


def main(argv: list[str]) -> None:
    """`brawl avalon-tts` 的薄入口：argv 全交给 TtsSettings 的 CLI source。"""
    asyncio.run(_run(TtsSettings.load(cli_args=argv), Console()))


__all__ = ["TtsSettings", "SettingsError", "main"]
