"""狼人杀生成路径的简单控制台渲染器。"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markup import escape

from werewolf import rules


class Renderer:
    def __init__(self) -> None:
        self.console = Console()

    def handle(self, ev: dict[str, Any]) -> None:
        getattr(self, f"_on_{ev['type']}", self._on_unknown)(ev)

    def _on_game_start(self, ev: dict[str, Any]) -> None:
        self.console.rule(f"[bold]狼人杀 · {ev['board_name']}[/bold]")
        self.console.print(f"配置：{ev['players']} 人 / 狼 {ev['wolves']} / 神 {ev['gods']} / 民 {ev['villagers']}")

    def _on_deal(self, ev: dict[str, Any]) -> None:
        for s in ev["seats"]:
            model = escape(f"[{s['model']}]")
            self.console.print(f"{s['no']}号 {s['persona']} {model} -> {rules.ROLE_LABEL.get(s['role'], s['role'])}")

    def _on_host(self, ev: dict[str, Any]) -> None:
        self.console.print(f"[dim]{ev['text']}[/dim]")

    def _on_speech(self, ev: dict[str, Any]) -> None:
        self.console.print(f"{ev['seat_no']}号：{ev['text']}")

    def _on_game_end(self, ev: dict[str, Any]) -> None:
        winner = "好人" if ev["winner"] == "good" else "狼人"
        self.console.rule(f"[bold]{winner}胜利[/bold] · {ev['win_reason']}")

    def _on_unknown(self, ev: dict[str, Any]) -> None:
        self.console.print(f"[dim]{ev['type']}[/dim]")
