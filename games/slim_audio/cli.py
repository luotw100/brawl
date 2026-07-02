"""`brawl slim-audio <replay_dir>`：把 replay/tts 里的 opus 替换成固定长度静音。

这是危险的覆盖型维护工具，入口必须人工确认；默认回答是 No。
"""

from __future__ import annotations

import io
import json
import os
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

import soundfile as sf
from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from rich.console import Console

_SAMPLE_RATE = 24_000


class SlimAudioError(Exception):
    """用户可见的输入/执行错误：CLI 只打印消息，不甩 traceback。"""


class ManifestPatch(Protocol):
    """manifest 更新抽象：没有 manifest 时也给执行阶段一个空操作实现。"""

    path: Path

    def update_durations(self, opus_names: set[str], duration_ms: int) -> int: ...

    def write(self) -> None: ...


class SlimAudioSettings(BaseSettings):
    """`brawl slim-audio` 的参数面，flag 从 Settings 派生。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_SLIM_AUDIO_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    dir: CliPositionalArg[Path]  # replay 目录，要求其中已有 tts/
    time: float = 3.0  # 静音长度，单位秒

    @model_validator(mode="after")
    def _check(self) -> SlimAudioSettings:
        if not self.dir.is_dir():
            raise ValueError(f"replay 目录不存在：{self.dir}")
        if not (self.dir / "tts").is_dir():
            raise ValueError(f"{self.dir} 缺 tts 目录")
        if self.time <= 0:
            raise ValueError(f"time 必须 >0 秒，收到 {self.time}")
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
                cli_prog_name="brawl slim-audio",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> SlimAudioSettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None


@dataclass
class JsonManifestPatch:
    """已有 manifest：只改本次覆盖到的 opus 条目的 duration_ms。"""

    path: Path
    data: dict[str, Any]

    def update_durations(self, opus_names: set[str], duration_ms: int) -> int:
        changed = 0
        for entry in self.data.values():
            if isinstance(entry, dict) and entry.get("file") in opus_names:
                entry["duration_ms"] = duration_ms
                changed += 1
        return changed

    def write(self) -> None:
        _atomic_write_bytes(self.path, json.dumps(self.data, ensure_ascii=False, indent=2).encode("utf-8"))


@dataclass(frozen=True)
class NullManifestPatch:
    """没有 manifest：执行阶段照常调用，实际不写任何东西。"""

    path: Path

    def update_durations(self, opus_names: set[str], duration_ms: int) -> int:
        return 0

    def write(self) -> None:
        return None


@dataclass(frozen=True)
class SlimAudioPlan:
    replay_dir: Path
    tts_dir: Path
    opus_files: tuple[Path, ...]
    duration_seconds: float
    duration_ms: int
    manifest: ManifestPatch

    @property
    def opus_names(self) -> set[str]:
        return {path.name for path in self.opus_files}


@dataclass(frozen=True)
class SlimAudioResult:
    files: int
    manifest_entries: int
    bytes_per_file: int


def build_plan(settings: SlimAudioSettings) -> SlimAudioPlan:
    tts_dir = settings.dir / "tts"
    opus_files = tuple(sorted(path for path in tts_dir.glob("*.opus") if path.is_file()))
    if not opus_files:
        raise SlimAudioError(f"{tts_dir} 下没有 .opus 音频")
    duration_ms = round(settings.time * 1000)
    return SlimAudioPlan(
        replay_dir=settings.dir,
        tts_dir=tts_dir,
        opus_files=opus_files,
        duration_seconds=settings.time,
        duration_ms=duration_ms,
        manifest=_load_manifest(tts_dir / "manifest.json"),
    )


def slim_audio(settings: SlimAudioSettings, console: Console, ask: Callable[[str], str] = input) -> SlimAudioResult | None:
    plan = build_plan(settings)
    _print_warning(plan, console)
    if ask("继续？输入 y 确认覆盖 [y/N]: ").strip().lower() != "y":
        console.print("已取消，未修改任何文件。")
        return None
    result = execute(plan)
    console.print(
        f"完成：已替换 {result.files} 个 opus 为 {plan.duration_seconds:g}s 静音；"
        f"manifest 更新 {result.manifest_entries} 条。"
    )
    return result


def execute(plan: SlimAudioPlan) -> SlimAudioResult:
    audio = _silent_opus(plan.duration_seconds)
    manifest_entries = plan.manifest.update_durations(plan.opus_names, plan.duration_ms)
    for path in plan.opus_files:
        _atomic_write_bytes(path, audio)
    plan.manifest.write()
    return SlimAudioResult(files=len(plan.opus_files), manifest_entries=manifest_entries, bytes_per_file=len(audio))


def _print_warning(plan: SlimAudioPlan, console: Console) -> None:
    console.print("[bold red]危险操作[/bold red]：将覆盖 replay 的 TTS 音频。")
    console.print(f"Replay：{plan.replay_dir}")
    console.print(f"目标：{plan.tts_dir} 下 {len(plan.opus_files)} 个 .opus")
    console.print(f"替换为：{plan.duration_seconds:g}s 静音 Opus")
    console.print(f"Manifest：{plan.manifest.path}")


def _load_manifest(path: Path) -> ManifestPatch:
    if not path.is_file():
        return NullManifestPatch(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SlimAudioError(f"manifest.json 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise SlimAudioError("manifest.json 顶层必须是对象")
    return JsonManifestPatch(path, data)


def _silent_opus(seconds: float) -> bytes:
    frames = max(1, round(seconds * _SAMPLE_RATE))
    samples = array("f", [0.0]) * frames
    buf = io.BytesIO()
    sf.write(buf, samples, _SAMPLE_RATE, format="OGG", subtype="OPUS")
    return buf.getvalue()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def main(argv: list[str]) -> None:
    slim_audio(SlimAudioSettings.load(argv), Console())


__all__ = ["SlimAudioError", "SlimAudioSettings", "build_plan", "execute", "main", "slim_audio"]
