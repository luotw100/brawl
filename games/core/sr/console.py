"""通用句子控制台渲染器：只读 sentences.jsonl，不依赖任何玩法语义。

这是 sentence runtime 的终端回放层。对局生成时（loop）零 wall-clock；回放时
注入 PacedTypist 即可逐字打字机。它通过 `deliveries[].channels` 区分公开广播 /
观众上帝视角， God-view 下 monologue 和结构化 payload 都会显示。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Protocol

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from core.sr.types import Channel, Sentence


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
        if char_delay <= 0:
            raise ValueError(f"char_delay 必须 >0，收到 {char_delay}")
        self._char_delay = char_delay

    def panel(self, console: Console, body: Text, title: str, border_style: str | None = None) -> None:
        if len(body) == 0:
            console.print(Panel(body, title=title, title_align="left", border_style=border_style))
            return
        with Live(console=console, auto_refresh=False) as live:
            for n in range(1, len(body) + 1):
                live.update(Panel(body[:n], title=title, title_align="left", border_style=border_style), refresh=True)
                import time

                time.sleep(self._char_delay)


class ConsoleRenderer:
    """读 sentences 并渲染到终端。玩法无关，只看 deliveries 的 text 和 channels。"""

    def __init__(
        self,
        *,
        typist: Typist | None = None,
        show_audience: bool = True,
        console: Console | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.console = console or Console()
        self.typist = typist or InstantTypist()
        self.show_audience = show_audience
        self.meta = meta

    def set_meta(self, meta: dict[str, Any] | None) -> None:
        self.meta = meta

    def render_sentence(self, sentence: Sentence) -> None:
        self._render_one(sentence.model_dump(), self._roles_from_meta(self.meta))

    def render(self, sentences: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> None:
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

    def _render_one(self, sent: dict[str, Any], roles: dict[int, str]) -> None:
        speaker = sent.get("speaker", "?")
        deliveries = sent.get("deliveries") or []
        body = Text()
        has_content = False
        for i, d in enumerate(deliveries):
            text = d.get("text", "")
            if not text:
                continue
            channels = d.get("channels") or []
            if "audience" in channels and not self.show_audience:
                continue
            has_content = True
            if i > 0:
                body.append("\n")
            body.append(f"{_channel_label(channels)} ", style="dim")
            if "audience" in channels:
                body.append(text, style="dim italic")
            else:
                body.append(text, style="bold")
        if not has_content:
            return

        title = _speaker_label(speaker, roles)
        border = self._border_style(speaker, roles)
        self.typist.panel(self.console, body, title, border_style=border)

    def _border_style(self, speaker: int | str, roles: dict[int, str]) -> str:
        if speaker == "host":
            return "cyan"
        if not isinstance(speaker, int):
            return ""
        role = roles.get(speaker, "")
        # 配色数据驱动：meta.setup.role_colors（玩法写入）优先；缺省回退到阿瓦隆旧 replay
        # 的内建表（它们生成时还没有 role_colors 字段）。role 查不到就无色。
        colors = ((self.meta or {}).get("setup") or {}).get("role_colors") or _LEGACY_ROLE_COLORS
        return colors.get(role, "")


# 旧阿瓦隆 replay 的角色配色（它们的 meta 没有 role_colors 字段，靠这张表自圆）。
_LEGACY_ROLE_COLORS = {
    "merlin": "blue", "percival": "blue", "servant": "blue",
    "assassin": "red", "morgana": "red", "mordred": "red", "oberon": "red", "minion": "red",
}


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="brawl console", description="渲染 replay 的 sentences.jsonl 到终端")
    parser.add_argument("replay_dir", type=Path, help="replay 目录（含 sentences.jsonl）")
    parser.add_argument("--pace", type=float, default=None, metavar="SEC", help="打字机每字延迟秒数")
    parser.add_argument("--no-audience", action="store_true", help="隐藏 audience 上帝视角内容")
    args = parser.parse_args(argv)

    replay_dir = Path(args.replay_dir)
    sent_path = replay_dir / "sentences.jsonl"
    meta_path = replay_dir / "meta.json"
    if not sent_path.exists():
        raise ValueError(f"不是 replay 目录，缺少 sentences.jsonl：{replay_dir}")

    sentences = [
        json.loads(ln)
        for ln in sent_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None

    typist: Typist = PacedTypist(args.pace) if args.pace is not None else InstantTypist()
    renderer = ConsoleRenderer(typist=typist, show_audience=not args.no_audience)
    renderer.render(sentences, meta)


if __name__ == "__main__":
    main([])
