"""`brawl avalon-tts <replay_dir>`：把一局 replay 的公开发言合成成音频。

消费端，像 avalon-replay 一样只读快照、零对局逻辑。cloud 音色从 meta.json 查
（setup.voices / setup.host_voice）；self-hosted 音色从
（setup.self_hosted_voices / setup.host_self_hosted_voice）查。旧 replay 没有 self-hosted
快照时，用 setup.cast / setup.host_cast 作为自托管服务端 voice name 兼容。

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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

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
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


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
    rps: float = 2.8  # DashScope 合成请求发起速率；qwen3-tts-vd 官方限流约 3 RPS，默认留一点余量
    backend: Literal["qwen3-tts-cloud", "qwen3-tts-self-hosted"] = "qwen3-tts-cloud"
    self_hosted_url: str = "http://127.0.0.1:8010"
    self_hosted_timeout: float = 300.0

    @model_validator(mode="after")
    def _check(self) -> TtsSettings:
        if not self.dir.is_dir():
            raise ValueError(f"replay 目录不存在：{self.dir}")
        for name in ("events.jsonl", "meta.json"):
            if not (self.dir / name).is_file():
                raise ValueError(f"{self.dir} 不是 replay 目录（缺 {name}）")
        if self.rps <= 0:
            raise ValueError(f"rps 必须 >0，收到 {self.rps}")
        if self.self_hosted_timeout <= 0:
            raise ValueError(f"self_hosted_timeout 必须 >0，收到 {self.self_hosted_timeout}")
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
    voice_id: str  # TTS 后端句柄：cloud/self-hosted 是 voice_id；旧 self-hosted replay 可用 persona id 兼容
    text: str


@dataclass
class LaunchClock:
    """按固定节拍发射请求；2.8 rps 就是 10 秒左右发完 28 条。"""

    rps: float
    _next_at: float = 0.0

    def __post_init__(self) -> None:
        self._interval = 1 / self.rps

    async def wait_next(self) -> None:
        now = time.monotonic()
        if self._next_at > now:
            await asyncio.sleep(self._next_at - now)
            now = time.monotonic()
        self._next_at = now + self._interval


@dataclass
class TtsJob:
    """一条 TTS 请求的展示状态。"""

    index: int
    utterance: Utterance
    status: str = "Queued"
    detail: str = ""
    attempt: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_ms: int = 0


class TtsDashboard:
    """Rich Live 渲染器：固定刷新一个框，避免长任务刷屏。"""

    def __init__(self, settings: TtsSettings, utterances: int, done: int, jobs: list[TtsJob], tts_dir: Path, console: Console, backend_label: str) -> None:
        self._settings = settings
        self._utterances = utterances
        self._already_done = done
        self._jobs = jobs
        self._by_call_id = {j.utterance.call_id: j for j in jobs}
        self._tts_dir = tts_dir
        self._console = console
        self._started_at = time.monotonic()
        self._latest = "等待首条音频"
        self._backend_label = backend_label

    def status(self, u: Utterance, status: str, detail: str = "", attempt: int | None = None) -> None:
        job = self._by_call_id[u.call_id]
        job.status = status
        job.detail = detail
        if attempt is not None:
            job.attempt = attempt
        if job.started_at == 0.0 and status != "Queued":
            job.started_at = time.monotonic()

    def done(self, u: Utterance, duration_ms: int) -> None:
        job = self._by_call_id[u.call_id]
        job.status = "Done"
        job.detail = f"{duration_ms / 1000:.1f}s"
        job.duration_ms = duration_ms
        job.ended_at = time.monotonic()
        self._latest = f"{u.speaker} · {u.persona} · {duration_ms / 1000:.1f}s"

    def render(self) -> Panel:
        now = time.monotonic()
        counts = {s: sum(1 for j in self._jobs if j.status == s) for s in ("Queued", "Request", "Download", "Transcode", "Done")}
        failed = sum(1 for j in self._jobs if j.status == "Retry")
        completed = self._already_done + counts["Done"]
        active = counts["Request"] + counts["Download"] + counts["Transcode"] + failed

        summary = Table.grid(expand=True)
        summary.add_column(style="bold cyan", ratio=1)
        summary.add_column(style="white", ratio=3)
        summary.add_column(style="bold cyan", ratio=1)
        summary.add_column(style="white", ratio=2)
        summary.add_row("Replay", str(self._settings.dir), "Model", self._backend_label)
        summary.add_row("Output", str(self._tts_dir), "RPS", f"{self._settings.rps:.2f}")
        summary.add_row("Done", f"{completed}/{self._utterances}", "Active", str(active))
        summary.add_row("Queued", str(counts["Queued"]), "Latest", self._latest)

        table = Table(box=box.SIMPLE_HEAVY, expand=True, show_edge=False)
        table.add_column("#", style="dim", width=4, justify="right")
        table.add_column("Speaker", style="bold", width=10)
        table.add_column("Persona", overflow="fold", ratio=1)
        table.add_column("State", width=10)
        table.add_column("Try", justify="right", width=4)
        table.add_column("Elapsed", justify="right", width=8)
        table.add_column("Detail", overflow="fold", ratio=2)

        for job in self._visible_jobs():
            table.add_row(
                str(job.index),
                job.utterance.speaker,
                job.utterance.persona,
                self._state_label(job.status),
                str(job.attempt),
                self._elapsed(job, now),
                job.detail or job.utterance.call_id,
            )

        body = Group(summary, table)
        elapsed = now - self._started_at
        return Panel(body, title=f"[bold]Avalon Voice Forge[/bold] [dim]{elapsed:0.1f}s[/dim]", border_style="bright_cyan", padding=(1, 2))

    def _visible_jobs(self) -> list[TtsJob]:
        height = max(8, self._console.size.height - 12)
        ordered = sorted(self._jobs, key=lambda j: (j.status == "Done", j.index))
        if len(ordered) <= height:
            return ordered
        return ordered[: height - 1] + [TtsJob(index=0, utterance=Utterance("more", "...", "...", "", ""), detail=f"{len(ordered) - height + 1} more")]

    def _elapsed(self, job: TtsJob, now: float) -> str:
        if job.status == "Queued" or job.started_at == 0.0:
            return "-"
        end = job.ended_at or now
        return f"{end - job.started_at:0.1f}s"

    def _state_label(self, status: str) -> str:
        styles = {
            "Queued": "[dim]Queued[/dim]",
            "Request": "[cyan]Request[/cyan]",
            "Download": "[magenta]Download[/magenta]",
            "Transcode": "[yellow]Transcode[/yellow]",
            "Retry": "[red]Retry[/red]",
            "Done": "[green]Done[/green]",
        }
        return styles.get(status, status)


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

    voices, host_voice = _voices(settings, meta)

    utterances = _utterances(events, voices, host_voice, meta["cast"], meta["host_cast"])

    # 边界前置校验：任一说话人缺 TTS 配置就别开工，免得合成到一半才炸。
    missing = sorted({f"{u.speaker}（{u.persona}）" for u in utterances if not u.voice_id})
    if missing:
        raise SettingsError(f"以下说话人 meta 里无 TTS 配置，无法合成：{'、'.join(missing)}")

    tts_dir = settings.dir / "tts"
    tts_dir.mkdir(exist_ok=True)
    manifest_path = tts_dir / "manifest.json"
    manifest: dict[str, dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}

    # 差集：manifest 里有记录且音频文件还在的，算已完成，跳过。
    todo = [u for u in utterances if not _is_done(u, settings.backend, manifest, tts_dir)]
    done = len(utterances) - len(todo)
    jobs = [TtsJob(i, u) for i, u in enumerate(todo, start=1)]
    backend_label = "AliCloudQwen3TTS" if settings.backend == "qwen3-tts-cloud" else "SelfHostedQwen3TTS"
    dashboard = TtsDashboard(settings, len(utterances), done, jobs, tts_dir, console, backend_label)
    if not todo:
        console.print(dashboard.render())
        return

    with Live(dashboard.render(), console=console, refresh_per_second=8, vertical_overflow="ellipsis") as live:
        if settings.backend == "qwen3-tts-cloud":
            clock = LaunchClock(settings.rps)
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {_api_key()}"}, timeout=60) as client:
                from avalon.tts_backend import AliCloudQwen3TTS

                synthesizer = AliCloudQwen3TTS(client)
                failed = await _synthesize_all(synthesizer, settings.backend, todo, dashboard, clock, tts_dir, manifest, manifest_path, live)
        else:
            from avalon.tts_backend import SelfHostedQwen3TTS

            timeout = httpx.Timeout(settings.self_hosted_timeout, connect=5)
            async with httpx.AsyncClient(timeout=timeout) as client:
                synthesizer = SelfHostedQwen3TTS(client, settings.self_hosted_url)
                await synthesizer.preflight({u.voice_id for u in todo})
                failed = await _synthesize_serial(synthesizer, settings.backend, todo, dashboard, tts_dir, manifest, manifest_path, live)

    if failed is not None:
        raise failed

    console.print(f"完成：音频与 manifest.json 已写入 {tts_dir}")


def _voices(settings: TtsSettings, meta: dict[str, Any]) -> tuple[list[str], str]:
    if settings.backend == "qwen3-tts-cloud":
        voices, host_voice = meta.get("voices"), meta.get("host_voice")
        missing_msg = "缺 setup.voices/host_voice"
    elif settings.backend == "qwen3-tts-self-hosted":
        voices, host_voice = meta.get("self_hosted_voices"), meta.get("host_self_hosted_voice")
        missing_msg = "缺 setup.self_hosted_voices/host_self_hosted_voice"
        if voices is None or host_voice is None:
            voices, host_voice = meta.get("cast"), meta.get("host_cast")
            missing_msg = "缺 setup.self_hosted_voices/host_self_hosted_voice 且缺 setup.cast/host_cast 兼容字段"
    if voices is None or host_voice is None:
        raise SettingsError(f"{settings.dir}/meta.json 不自包含音色（{missing_msg}）")
    return voices, host_voice


def _is_done(u: Utterance, backend: str, manifest: dict[str, dict[str, Any]], tts_dir: Path) -> bool:
    record = manifest.get(u.call_id)
    if record is None or not (tts_dir / f"{u.call_id}.opus").is_file():
        return False
    return record.get("backend", "qwen3-tts-cloud") == backend


async def _synthesize_all(
    synthesizer: object,
    backend: str,
    todo: list[Utterance],
    dashboard: TtsDashboard,
    clock: LaunchClock,
    tts_dir: Path,
    manifest: dict[str, dict[str, Any]],
    manifest_path: Path,
    live: Live,
) -> Exception | None:
    """共用合成循环：发射请求、收拢结果、刷 manifest。不同后端的差异全收在 synthesizer 里。"""
    in_flight: set[asyncio.Task[tuple[Utterance, int]]] = set()
    failed: Exception | None = None

    async def one(u: Utterance) -> tuple[Utterance, int]:
        wav = await synthesizer.synthesize(dashboard, u)
        dashboard.status(u, "Transcode", "opus")
        opus, duration_ms = _to_opus(wav)
        _atomic_write_bytes(tts_dir / f"{u.call_id}.opus", opus)
        return u, duration_ms

    for u in todo:
        await clock.wait_next()
        task = asyncio.create_task(one(u))
        in_flight.add(task)
        live.update(dashboard.render())
        done_tasks = {t for t in in_flight if t.done()}
        in_flight.difference_update(done_tasks)
        failed = _collect_done(done_tasks, backend, manifest, manifest_path, dashboard)
        live.update(dashboard.render())
        if failed is not None:
            break

    while in_flight and failed is None:
        done_tasks, _ = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
        in_flight.difference_update(done_tasks)
        failed = _collect_done(done_tasks, backend, manifest, manifest_path, dashboard)
        live.update(dashboard.render())

    if failed is not None:
        for task in in_flight:
            task.cancel()
        await asyncio.gather(*in_flight, return_exceptions=True)

    return failed


async def _synthesize_serial(
    synthesizer: object,
    backend: str,
    todo: list[Utterance],
    dashboard: TtsDashboard,
    tts_dir: Path,
    manifest: dict[str, dict[str, Any]],
    manifest_path: Path,
    live: Live,
) -> Exception | None:
    for u in todo:
        task = asyncio.create_task(_one(synthesizer, u, dashboard, tts_dir))
        live.update(dashboard.render())
        await asyncio.wait({task})
        failed = _collect_done({task}, backend, manifest, manifest_path, dashboard)
        live.update(dashboard.render())
        if failed is not None:
            return failed
    return None


async def _one(synthesizer: object, u: Utterance, dashboard: TtsDashboard, tts_dir: Path) -> tuple[Utterance, int]:
    wav = await synthesizer.synthesize(dashboard, u)
    dashboard.status(u, "Transcode", "opus")
    opus, duration_ms = _to_opus(wav)
    _atomic_write_bytes(tts_dir / f"{u.call_id}.opus", opus)
    return u, duration_ms


def _collect_done(
    tasks: list[asyncio.Task[tuple[Utterance, int]]] | set[asyncio.Task[tuple[Utterance, int]]],
    backend: str,
    manifest: dict[str, dict[str, Any]],
    manifest_path: Path,
    dashboard: TtsDashboard,
) -> Exception | None:
    for task in tasks:
        try:
            u, duration_ms = task.result()
        except Exception as e:
            return e
        manifest[u.call_id] = {
            "speaker": u.speaker,
            "persona": u.persona,
            "backend": backend,
            "voice_id": u.voice_id,
            "text": u.text,
            "file": f"{u.call_id}.opus",
            "duration_ms": duration_ms,
        }
        # 每条原子刷 manifest：中断也不丢已完成的部分，且绝不留半截 manifest。
        _atomic_write_bytes(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        dashboard.done(u, duration_ms)
    return None


def main(argv: list[str]) -> None:
    """`brawl avalon-tts` 的薄入口：argv 全交给 TtsSettings 的 CLI source。"""
    asyncio.run(_run(TtsSettings.load(cli_args=argv), Console()))


__all__ = ["TtsSettings", "SettingsError", "main"]
