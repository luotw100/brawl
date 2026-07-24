"""通用句子控制台渲染器：只读 sentences.jsonl，不依赖任何玩法语义。

这是 sentence runtime 的终端回放层。对局生成时（loop）零 wall-clock；回放时
注入 PacedTypist 即可逐字打字机。它通过 `deliveries[].channels` 区分公开广播 /
观众上帝视角， God-view 下 monologue 和结构化 payload 都会显示。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, ClassVar, Protocol

from pydantic import Field, model_validator
from pydantic_settings import CliPositionalArg, SettingsConfigDict
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from brawl.runtime.cli_settings import CliSettings
from brawl.runtime.files import UnsafePathError, read_regular_bytes
from brawl.runtime.journal import JournalCorruptionError, load_replay_sentences
from brawl.runtime.types import Channel, Sentence


class ConsoleError(Exception):
    """终端回放输入不是一份可安全读取的新 replay。"""


def _speaker_label(speaker: int | str, roles: dict[int, str] | None = None) -> str:
    if speaker == "host":
        return "主持人"
    label = f"{speaker}号"
    if roles and isinstance(speaker, int) and speaker in roles:
        label += f"（{roles[speaker]}）"
    return label


def _channel_label(chs: list[Channel]) -> str:
    """把路由目标转成人类可读的“谁能听到”标签。"""
    return " ".join(_label_one(ch) for ch in chs)


def _label_one(ch: Channel) -> str:
    if ch == "broadcast":
        return "📢公开"
    if ch == "audience":
        return "👁上帝视角"
    return f"🔒{ch}号私聊"


class Typist(Protocol):
    """把一段文本面板吐到屏上的策略：Instant（瞬出）或 Paced（打字机）。"""

    def panel(self, console: Console, body: Text, title: str, border_style: str | None = None) -> None: ...


class InstantTypist:
    """整段一次性打印——生成路径与快速回放用它，不引入 sleep。"""

    def panel(self, console: Console, body: Text, title: str, border_style: str | None = None) -> None:
        console.print(Panel(body, title=title, title_align="left", border_style=border_style))


class PacedTypist:
    """打字机：逐字长出面板。仅回放层注入。"""

    def __init__(self, char_delay: float) -> None:
        if not math.isfinite(char_delay) or char_delay <= 0:
            raise ValueError(f"char_delay 必须是有限正数，收到 {char_delay}")
        self._char_delay = char_delay

    def panel(self, console: Console, body: Text, title: str, border_style: str | None = None) -> None:
        if len(body) == 0:
            console.print(Panel(body, title=title, title_align="left", border_style=border_style))
            return
        with Live(console=console, auto_refresh=False) as live:
            for n in range(1, len(body) + 1):
                live.update(Panel(body[:n], title=title, title_align="left", border_style=border_style), refresh=True)
                time.sleep(self._char_delay)


class ConsoleRenderer:
    """读 sentences 并渲染到终端。玩法无关，只看 deliveries 的 text 和 channels。"""

    def __init__(
        self,
        *,
        typist: Typist | None = None,
        console: Console | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.console = console or Console()
        self.typist = typist or InstantTypist()
        self.meta = meta

    def set_meta(self, meta: dict[str, Any] | None) -> None:
        self.meta = meta

    def render_sentence(self, sentence: Sentence) -> None:
        self._render_one(sentence, self._roles_from_meta(self.meta))

    def render(self, sentences: list[Sentence], meta: dict[str, Any] | None = None) -> None:
        self.set_meta(meta)
        roles = self._roles_from_meta(self.meta)
        for sent in sentences:
            self._render_one(sent, roles)

    def _roles_from_meta(self, meta: dict[str, Any] | None) -> dict[int, str]:
        if not meta:
            return {}
        setup = meta.get("setup") or {}
        roles = setup.get("roles") or []
        return {i + 1: r for i, r in enumerate(roles)}

    def _render_one(self, sent: Sentence, roles: dict[int, str]) -> None:
        speaker = sent.speaker
        body = Text()
        has_content = False
        for delivery in sent.deliveries:
            text = delivery.text
            if not text:
                continue
            channels = delivery.channels
            if has_content:
                body.append("\n")
            has_content = True
            body.append(f"{_channel_label(channels)} ", style="dim")
            if "audience" in channels and "broadcast" not in channels:
                body.append(text, style="dim italic")
            else:
                body.append(text, style="bold")
        if not has_content:
            return

        title = _speaker_label(speaker, roles)
        border = "cyan" if speaker == "host" else ""
        self.typist.panel(self.console, body, title, border_style=border)


class ConsoleSettings(CliSettings):
    """在终端回放一局。默认整段瞬出；--pace 开启打字机效果。"""

    CLI_PROG: ClassVar[str] = "brawl console"
    model_config = SettingsConfigDict(
        env_prefix="BRAWL_CONSOLE_",
        cli_kebab_case=True,
        extra="forbid",
    )

    replay_dir: CliPositionalArg[Path] = Field(description="replay 目录（含 sentences.jsonl）")
    pace: float | None = Field(default=None, description="打字机每字间隔秒数（如 0.03）；缺省整段瞬出")

    @model_validator(mode="after")
    def _check(self) -> ConsoleSettings:
        if self.pace is not None and (not math.isfinite(self.pace) or self.pace <= 0):
            raise ValueError(f"--pace 必须是正数秒数，收到 {self.pace}")
        return self


def main(argv: list[str]) -> None:
    settings = ConsoleSettings.load(argv)
    replay_dir = settings.replay_dir
    try:
        sentences = load_replay_sentences(replay_dir)
    except (OSError, UnsafePathError, JournalCorruptionError) as exc:
        raise ConsoleError(f"无法读取 replay 的 Sentence Runtime 日志：{exc}") from exc
    meta = _load_optional_meta(replay_dir / "meta.json")
    if meta is not None and not isinstance(meta, dict):
        raise ConsoleError("meta.json 顶层必须是对象")

    typist: Typist = PacedTypist(settings.pace) if settings.pace is not None else InstantTypist()
    renderer = ConsoleRenderer(typist=typist)
    renderer.render(sentences, meta)


def _load_optional_meta(path: Path) -> dict[str, Any] | None:
    try:
        raw = read_regular_bytes(path)
    except FileNotFoundError:
        return None
    except (OSError, UnsafePathError) as exc:
        raise ConsoleError(f"无法安全读取 meta.json：{exc}") from exc
    try:
        meta = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConsoleError(f"meta.json 不是合法 JSON：{exc}") from exc
    return meta
