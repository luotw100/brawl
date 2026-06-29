"""狼人杀控制台渲染器。

生成路径只消费事件流，不参与游戏逻辑。当前是上帝视角：发牌、夜晚行动、独白、
暗投票都会展示；闭眼/观众视角后续在上层统一处理。
"""

from __future__ import annotations

from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from werewolf import rules

_SIDE_STYLE = {rules.Side.GOOD.value: "blue", rules.Side.WEREWOLF.value: "red"}
# 文案标签从 rules 取单一来源（与 prompts 同源），console 只额外管渲染样式。
_SIDE_LABEL = {side.value: label for side, label in rules.SIDE_LABEL.items()}
_DEATH_REASON = rules.DEATH_REASON_LABEL
_WIN_REASON = rules.WIN_REASON_LABEL

_PHASE_LABEL = {
    "last_words": "遗言",
    "discussion": "发言",
}


def _seat(no: int | None) -> str:
    return "无" if no is None else f"{no}号"


def _role_label(role: str | rules.Role | None) -> str:
    if role is None:
        return "未知"
    try:
        role_key = role if isinstance(role, rules.Role) else rules.Role(role)
    except ValueError:
        return str(role)
    return rules.ROLE_LABEL[role_key]


def _side_label(side: str | rules.Side | None) -> str:
    if side is None:
        return "未知阵营"
    side_value = side.value if isinstance(side, rules.Side) else str(side)
    return _SIDE_LABEL.get(side_value, side_value)


def _side_style(side: str | rules.Side | None) -> str:
    side_value = side.value if isinstance(side, rules.Side) else str(side)
    return _SIDE_STYLE.get(side_value, "white")


def _death_reason(reason: str | None) -> str:
    if reason is None:
        return "未知"
    return _DEATH_REASON.get(reason, reason)


def _monologue(text: str | None) -> Text:
    line = Text()
    if text:
        line.append(f"内心：{text}", style="dim italic")
    else:
        line.append("内心：-", style="dim")
    return line


def _speech_body(text: str | None, monologue: str | None = None) -> Text:
    body = Text()
    body.append(text or "（无发言）", style="bold")
    if monologue is not None:
        body.append("\n")
        body.append_text(_monologue(monologue))
    return body


class Renderer:
    """把狼人杀事件流渲染到终端。"""

    def __init__(self) -> None:
        self.console = Console()

    def handle(self, ev: dict[str, Any]) -> None:
        getattr(self, f"_on_{ev['type']}", self._on_unknown)(ev)

    def _on_game_start(self, ev: dict[str, Any]) -> None:
        self.console.rule(f"[bold]狼人杀 · {ev['board_name']}[/bold]")
        self.console.print(f"配置：{ev['players']} 人 / 狼 {ev['wolves']} / 神 {ev['gods']} / 民 {ev['villagers']}")

    def _on_deal(self, ev: dict[str, Any]) -> None:
        table = Table(title="发牌（上帝视角）")
        table.add_column("座位", justify="right")
        table.add_column("人设")
        table.add_column("流派")
        table.add_column("阵营")
        table.add_column("角色")
        table.add_column("模型")
        for seat in ev["seats"]:
            side = seat["side"]
            table.add_row(
                str(seat["no"]),
                seat["persona"],
                seat["strategy"],
                Text(_side_label(side), style=_side_style(side)),
                _role_label(seat["role"]),
                seat["model"] or "-",
            )
        self.console.print(table)

    def _on_host(self, ev: dict[str, Any]) -> None:
        self.console.print(Text(ev["text"], style="dim cyan"))

    def _on_night_start(self, ev: dict[str, Any]) -> None:
        self.console.rule(f"[bold blue]第 {ev['day']} 夜[/bold blue]")

    def _on_wolf_kill(self, ev: dict[str, Any]) -> None:
        table = Table(title=f"第 {ev['day']} 夜 · 狼人行动（上帝视角）")
        table.add_column("狼人", justify="right")
        table.add_column("刀口", justify="right")
        table.add_column("狼队发言")
        table.add_column("内心戏")
        for vote in ev["votes"]:
            table.add_row(
                _seat(vote["seat_no"]),
                _seat(vote["target_no"]),
                vote.get("speech") or "-",
                Text(vote.get("monologue") or "-", style="dim italic"),
            )
        self.console.print(table)
        self.console.print(Panel(Text(f"最终刀口：{_seat(ev['target_no'])}", style="bold red"), title="狼刀", title_align="left"))

    def _on_seer_check(self, ev: dict[str, Any]) -> None:
        result = ev["result"]
        body = Text()
        body.append(f"查验目标：{_seat(ev['target_no'])}\n", style="bold")
        body.append("查验结果：", style="bold")
        body.append(_side_label(result), style=f"bold {_side_style(result)}")
        body.append("\n")
        body.append_text(_monologue(ev.get("monologue")))
        self.console.print(Panel(body, title=f"预言家 {ev['seat_no']}号", title_align="left", border_style="blue"))

    def _on_witch_action(self, ev: dict[str, Any]) -> None:
        body = Text()
        body.append(f"解药：{'使用' if ev['save'] else '未使用'}\n", style="bold")
        body.append(f"毒药目标：{_seat(ev['poison_target_no'])}\n", style="bold")
        body.append_text(_monologue(ev.get("monologue")))
        self.console.print(Panel(body, title=f"女巫 {ev['seat_no']}号", title_align="left", border_style="magenta"))

    def _on_death_resolved(self, ev: dict[str, Any]) -> None:
        deaths = ev["deaths"]
        if not deaths:
            self.console.print(Panel(Text("平安夜", style="bold green"), title=f"第 {ev['day']} 天死亡结算", title_align="left"))
            return

        table = Table(title=f"第 {ev['day']} 天死亡结算")
        table.add_column("座位", justify="right")
        table.add_column("身份")
        table.add_column("死因")
        for death in deaths:
            table.add_row(_seat(death["seat_no"]), _role_label(death.get("role")), _death_reason(death.get("reason")))
        self.console.print(table)

    def _on_role_revealed(self, ev: dict[str, Any]) -> None:
        body = Text()
        body.append(f"{_seat(ev['seat_no'])} 公开身份：", style="bold")
        body.append(_role_label(ev["role"]), style="bold")
        body.append(f"（{_death_reason(ev.get('reason'))}）", style="dim")
        self.console.print(body)

    def _on_speech(self, ev: dict[str, Any]) -> None:
        phase = _PHASE_LABEL.get(ev["phase"], ev["phase"])
        border_style = "yellow" if ev["phase"] == "last_words" else "cyan"
        title = f"第 {ev['day']} 天 · {phase} · {_seat(ev['seat_no'])}"
        self.console.print(Panel(_speech_body(ev.get("text"), ev.get("monologue")), title=title, title_align="left", border_style=border_style))

    def _on_vote_result(self, ev: dict[str, Any]) -> None:
        table = Table(title=f"第 {ev['day']} 天 · 放逐投票")
        table.add_column("投票人", justify="right")
        table.add_column("目标", justify="right")
        table.add_column("内心戏")
        for vote in ev["votes"]:
            table.add_row(_seat(vote["seat_no"]), _seat(vote["target_no"]), Text(vote.get("monologue") or "-", style="dim italic"))
        self.console.print(table)

        if ev["exiled"] is not None:
            verdict = Text(f"放逐结果：{_seat(ev['exiled'])} 出局", style="bold red")
        elif ev["tied"]:
            verdict = Text("放逐结果：平票，无人出局", style="bold yellow")
        else:
            verdict = Text("放逐结果：无人出局", style="bold yellow")
        self.console.print(Panel(verdict, title="开票", title_align="left"))

    def _on_idiot_revealed(self, ev: dict[str, Any]) -> None:
        self.console.print(Panel(Text(f"{_seat(ev['seat_no'])} 翻牌为白痴，免于放逐。", style="bold yellow"), title="白痴翻牌", title_align="left"))

    def _on_hunter_shot(self, ev: dict[str, Any]) -> None:
        body = _speech_body(ev.get("speech"), ev.get("monologue"))
        body.append("\n")
        body.append(f"开枪目标：{_seat(ev['target_no'])}", style="bold red" if ev["target_no"] is not None else "bold")
        self.console.print(Panel(body, title=f"猎人 {ev['hunter_no']}号", title_align="left", border_style="red"))

    def _on_banter(self, ev: dict[str, Any]) -> None:
        side = ev["side"]
        title = f"复盘 · {_seat(ev['seat_no'])}（{_side_label(side)}·{_role_label(ev.get('role'))}）"
        self.console.print(Panel(_speech_body(ev.get("text"), ev.get("monologue")), title=title, title_align="left", border_style=_side_style(side)))

    def _on_game_end(self, ev: dict[str, Any]) -> None:
        winner = ev["winner"]
        label = _side_label(winner)
        style = _side_style(winner)
        reason = _WIN_REASON.get(ev["win_reason"], ev["win_reason"])
        self.console.rule(f"[bold {style}]{label}胜利[/bold {style}] · {reason}")

        renderables: list[Any] = []
        if ev["deaths"]:
            death_track = "  ".join(
                f"第{death['day']}天 {_seat(death['seat_no'])}/{_role_label(death.get('role'))}/{_death_reason(death.get('reason'))}"
                for death in ev["deaths"]
            )
            renderables.append(Text(f"死亡记录：{death_track}", style="dim"))

        table = Table(title="赛后揭晓")
        table.add_column("座位", justify="right")
        table.add_column("人设")
        table.add_column("阵营")
        table.add_column("角色")
        table.add_column("状态")
        table.add_column("模型")
        for seat in ev["seats"]:
            side = seat["side"]
            table.add_row(
                str(seat["no"]),
                seat["persona"],
                Text(_side_label(side), style=_side_style(side)),
                _role_label(seat["role"]),
                Text("存活", style="green") if seat["alive"] else Text("出局", style="red"),
                seat["model"] or "-",
            )
        renderables.append(table)
        self.console.print(Group(*renderables))

    def _on_unknown(self, ev: dict[str, Any]) -> None:
        self.console.print(Text(f"未渲染事件：{ev['type']}", style="dim"))
