"""控制台渲染器：订阅出向事件流，用 rich 美化输出（不直接 print）。

这是 stage(画面层) 的终端版雏形——只消费事件、不参与游戏逻辑。将来真正的 stage
换一套渲染即可，事件契约不变。
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_ROLE_LABEL = {"civilian": "平民", "undercover": "卧底", "blank": "白板"}
_ROLE_STYLE = {"civilian": "green", "undercover": "red", "blank": "yellow"}


class Renderer:
    """把事件流渲染到终端。god-view：词、身份、独白都展示。"""

    def __init__(self) -> None:
        self.console = Console()

    def handle(self, ev: dict[str, Any]) -> None:
        getattr(self, f"_on_{ev['type']}", self._on_unknown)(ev)

    # ---- 各事件 ----

    def _on_game_start(self, ev: dict[str, Any]) -> None:
        self.console.rule("[bold]谁是卧底 · 开局[/bold]")
        self.console.print(f"配置：{ev['players']} 人 / 卧底 {ev['undercover']} / 白板 {ev['blank']}")

    def _on_deal(self, ev: dict[str, Any]) -> None:
        table = Table(title=f"发牌（上帝视角）  平民词「{ev['civ_word']}」 vs 卧底词「{ev['undercover_word']}」")
        table.add_column("座位")
        table.add_column("人设")
        table.add_column("身份")
        table.add_column("词")
        table.add_column("模型")
        for s in ev["seats"]:
            role = s["role"]
            table.add_row(
                str(s["no"]),
                s["name"],
                Text(_ROLE_LABEL.get(role, role), style=_ROLE_STYLE.get(role, "white")),
                s["word"] or "—",
                s["model"] or "—",
            )
        self.console.print(table)

    def _on_round_start(self, ev: dict[str, Any]) -> None:
        self.console.rule(f"第 {ev['round']} 轮 · 描述")

    def _on_describe(self, ev: dict[str, Any]) -> None:
        body = Text()
        body.append(f"{ev['text']}\n", style="bold")
        body.append(f"💭 {ev['monologue']}", style="dim italic")
        self.console.print(Panel(body, title=f"{ev['seat_no']}号 {ev['name']}", title_align="left"))

    def _on_vote(self, ev: dict[str, Any]) -> None:
        line = Text()
        line.append(f"{ev['seat_no']}号 {ev['name']} → 投 {ev['target_no']}号 {ev['target_name']}：", style="bold")
        line.append(ev["reason"])
        self.console.print(line)
        self.console.print(Text(f"   💭 {ev['monologue']}", style="dim italic"))

    def _on_tally(self, ev: dict[str, Any]) -> None:
        counts = "  ".join(f"{no}号×{c}" for no, c in sorted(ev["counts"].items()))
        tie = "（平票，裁判随机）" if ev["tie"] else ""
        role = ev["eliminated_role"]
        out = Text()
        out.append(f"票数：{counts}{tie}\n")
        out.append(f"出局：{ev['eliminated_no']}号 {ev['eliminated_name']} —— ", style="bold")
        out.append(_ROLE_LABEL.get(role, role), style=_ROLE_STYLE.get(role, "white"))
        self.console.print(Panel(out, title="计票出局", title_align="left"))

    def _on_game_end(self, ev: dict[str, Any]) -> None:
        winner = "平民胜利" if ev["winner"] == "civilians" else "卧底胜利"
        style = "green" if ev["winner"] == "civilians" else "red"
        self.console.rule(f"[bold {style}]{winner}[/bold {style}] · 共 {ev['rounds']} 轮")
        table = Table(title="赛后揭晓")
        table.add_column("座位")
        table.add_column("人设")
        table.add_column("身份")
        table.add_column("词")
        table.add_column("结局")
        for s in ev["seats"]:
            role = s["role"]
            table.add_row(
                str(s["no"]),
                s["name"],
                Text(_ROLE_LABEL.get(role, role), style=_ROLE_STYLE.get(role, "white")),
                s["word"] or "—",
                "存活" if s["alive"] else "出局",
            )
        self.console.print(table)

    def _on_unknown(self, ev: dict[str, Any]) -> None:
        self.console.print(f"[dim]· {ev}[/dim]")
