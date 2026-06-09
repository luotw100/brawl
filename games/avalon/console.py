"""控制台渲染器：订阅出向事件流，用 rich 美化输出（不直接 print）。

这是 stage(画面层) 的终端版雏形——只消费事件、不参与游戏逻辑。将来真正的 stage
换一套渲染即可，事件契约不变。称呼一律"几号"（真实对局没人叫名字）；persona 列
是占位配置，仅上帝视角展示。
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def _panel_kw(title: str, border_style: str | None) -> dict[str, Any]:
    """统一的面板参数；border_style=None 时不传（rich 会拿默认边框，传 None 会炸）。"""
    kw: dict[str, Any] = {"title": title, "title_align": "left"}
    if border_style is not None:
        kw["border_style"] = border_style
    return kw


class Typist(Protocol):
    """把一段台词面板吐到屏上的策略——抽出来是为了让"是否逐字播放"成为注入项，
    而非写死在 Renderer 里。生成对局时用 InstantTypist(整段瞬出、零 wall-clock，
    守住"生成器不计时"不变量)；回放时注入 PacedTypist(逐字打字机)。"""

    def panel(self, console: Console, body: Text, title: str, border_style: str | None = None) -> None: ...


class InstantTypist:
    """整段一次性打印——Renderer 的默认行为，不引入任何 sleep。生成路径只用它。"""

    def panel(self, console: Console, body: Text, title: str, border_style: str | None = None) -> None:
        console.print(Panel(body, **_panel_kw(title, border_style)))


class PacedTypist:
    """打字机：在带框面板里逐字长出台词（rich.Live 重绘）。仅回放层注入。

    char_delay = 每字停顿秒数(由回放的 pace 旋钮换算)；面板长度由文本长度自然决定停留时长，
    故"按字数模拟阅读节奏"无需另算——逐字本身就是它。"""

    def __init__(self, char_delay: float) -> None:
        self._char_delay = char_delay

    def panel(self, console: Console, body: Text, title: str, border_style: str | None = None) -> None:
        kw = _panel_kw(title, border_style)
        if len(body) == 0:
            console.print(Panel(body, **kw))
            return
        with Live(console=console, auto_refresh=False) as live:
            for n in range(1, len(body) + 1):
                live.update(Panel(body[:n], **kw), refresh=True)
                time.sleep(self._char_delay)

_ROLE_LABEL = {
    "merlin": "梅林", "percival": "派西维尔", "servant": "忠臣",
    "assassin": "刺客", "morgana": "莫甘娜", "mordred": "莫德雷德", "oberon": "奥伯伦", "minion": "爪牙",
}
_SIDE_LABEL = {"good": "蓝方", "evil": "红方"}
_SIDE_STYLE = {"good": "blue", "evil": "red"}

_WIN_REASON = {
    "quests": "红方破坏满 3 个任务",
    "rejections": "连续 5 次否决组队",
    "assassination_hit": "刺客一击命中梅林",
    "assassination_miss": "刺客指认落空，梅林安然",
}


class Renderer:
    """把事件流渲染到终端。god-view：角色、情报、独白、暗牌都展示。"""

    def __init__(self, typist: Typist | None = None) -> None:
        self.console = Console()
        # 台词面板经 typist 吐屏；缺省瞬出(等价旧行为，生成路径零 wall-clock)。
        self.typist = typist if typist is not None else InstantTypist()

    def handle(self, ev: dict[str, Any]) -> None:
        getattr(self, f"_on_{ev['type']}", self._on_unknown)(ev)

    # ---- 各事件 ----

    def _on_game_start(self, ev: dict[str, Any]) -> None:
        self.console.rule("[bold]阿瓦隆 · 开局[/bold]")
        self.console.print(f"配置：{ev['players']} 人 / 蓝方 {ev['good']} / 红方 {ev['evil']}")

    def _on_deal(self, ev: dict[str, Any]) -> None:
        table = Table(title="发牌（上帝视角）")
        table.add_column("座位")
        table.add_column("人设")
        table.add_column("流派")
        table.add_column("阵营")
        table.add_column("角色")
        table.add_column("夜晚情报")
        table.add_column("模型")
        for s in ev["seats"]:
            side, role = s["side"], s["role"]
            table.add_row(
                str(s["no"]),
                s["persona"],
                s["strategy"],
                Text(_SIDE_LABEL.get(side, side), style=_SIDE_STYLE.get(side, "white")),
                _ROLE_LABEL.get(role, role),
                "看到 " + "、".join(f"{no}号" for no in s["intel"]) if s["intel"] else "—",
                s["model"] or "—",
            )
        self.console.print(table)
        lady = f"　湖中仙女：{ev['lady_holder']}号持有" if ev["lady_holder"] is not None else ""
        self.console.print(f"首任队长：{ev['leader_no']}号{lady}")

    def _on_propose(self, ev: dict[str, Any]) -> None:
        team = "、".join(f"{no}号" for no in ev["team"])
        self.console.rule(f"任务 {ev['quest']} · 第 {ev['attempt']} 次组队 [dim]· 队伍 {team}[/dim]")
        body = Text()
        body.append(f"{ev['speech']}\n", style="bold")
        body.append(f"💭 {ev['monologue']}", style="dim italic")
        self.typist.panel(self.console, body, title=f"队长 {ev['leader_no']}号")

    def _on_discuss(self, ev: dict[str, Any]) -> None:
        body = Text()
        body.append(f"{ev['text']}\n", style="bold")
        body.append(f"💭 {ev['monologue']}", style="dim italic")
        self.typist.panel(self.console, body, title=f"{ev['seat_no']}号")

    def _on_vote_result(self, ev: dict[str, Any]) -> None:
        for v in ev["votes"]:
            mark = Text("✔ 赞成", style="green") if v["approve"] else Text("✘ 反对", style="red")
            line = Text(f"{v['seat_no']}号 ", style="bold")
            line.append_text(mark)
            self.console.print(line)
            self.console.print(Text(f"   💭 {v['monologue']}", style="dim italic"))
        total = len(ev["votes"])
        if ev["approved"]:
            verdict = Text(f"表决 {ev['approves']}/{total}：通过，任务出发！", style="bold green")
        else:
            verdict = Text(f"表决 {ev['approves']}/{total}：否决（本任务已连否 {ev['rejections']} 次）", style="bold red")
        self.console.print(Panel(verdict, title="开票", title_align="left"))

    def _on_quest_result(self, ev: dict[str, Any]) -> None:
        body = Text()
        for c in ev["cards"]:
            card = Text("成功", style="blue") if c["success"] else Text("破坏", style="red")
            body.append(f"{c['seat_no']}号 暗出 ", style="bold")
            body.append_text(card)
            body.append(f"  💭 {c['monologue']}\n", style="dim italic")
        result_zh = "成功" if ev["result"] == "success" else "失败"
        style = "blue" if ev["result"] == "success" else "red"
        body.append(f"破坏票 {ev['fails']} 张（{ev['fails_required']} 张即失败）→ 任务{result_zh}", style=f"bold {style}")
        body.append(f"\n任务轨：蓝 {ev['succeeded']} : {ev['failed']} 红", style="bold")
        self.console.print(Panel(body, title=f"任务 {ev['quest']} 开牌（上帝视角）", title_align="left"))

    def _on_lady(self, ev: dict[str, Any]) -> None:
        truth = ev["truth"]
        body = Text()
        body.append(f"查验 {ev['target_no']}号 → 真实阵营：", style="bold")
        body.append(_SIDE_LABEL.get(truth, truth), style=_SIDE_STYLE.get(truth, "white"))
        body.append("（只有持有者看到）\n", style="dim")
        body.append(f"💭 {ev['choose_monologue']}\n", style="dim italic")
        body.append(f"当众宣布：{ev['speech']}\n", style="bold")
        body.append(f"💭 {ev['monologue']}", style="dim italic")
        self.typist.panel(self.console, body, title=f"湖中仙女 · 持有者 {ev['holder_no']}号")

    def _on_assassination(self, ev: dict[str, Any]) -> None:
        self.console.rule("[bold red]刺杀时刻[/bold red]")
        body = Text()
        body.append(f"{ev['speech']}\n", style="bold")
        body.append(f"💭 {ev['monologue']}\n", style="dim italic")
        verdict = "命中！梅林倒下" if ev["hit"] else f"落空——{ev['target_no']}号不是梅林"
        body.append(f"刺向 {ev['target_no']}号：{verdict}", style="bold red" if ev["hit"] else "bold blue")
        self.typist.panel(self.console, body, title=f"刺客 {ev['assassin_no']}号")

    def _on_game_end(self, ev: dict[str, Any]) -> None:
        side = ev["winner"]
        reason_key = ev["win_reason"]
        if reason_key == "assassination":
            reason_key = "assassination_hit" if side == "evil" else "assassination_miss"
        label = _SIDE_LABEL.get(side, side)
        style = _SIDE_STYLE.get(side, "white")
        self.console.rule(f"[bold {style}]{label}胜利[/bold {style}] · {_WIN_REASON.get(reason_key, reason_key)}")
        track = "  ".join(
            f"任务{q['quest']}:{'✔' if q['result'] == 'success' else '✘'}(破坏{q['fails']})" for q in ev["quests"]
        )
        if track:
            self.console.print(f"任务轨：{track}")
        table = Table(title="赛后揭晓")
        table.add_column("座位")
        table.add_column("人设")
        table.add_column("阵营")
        table.add_column("角色")
        table.add_column("模型")
        for s in ev["seats"]:
            table.add_row(
                str(s["no"]),
                s["persona"],
                Text(_SIDE_LABEL.get(s["side"], s["side"]), style=_SIDE_STYLE.get(s["side"], "white")),
                _ROLE_LABEL.get(s["role"], s["role"]),
                s["model"] or "—",
            )
        self.console.print(table)

    def _on_banter(self, ev: dict[str, Any]) -> None:
        body = Text()
        body.append(f"{ev['text']}\n", style="bold")
        body.append(f"💭 {ev['monologue']}", style="dim italic")
        title = f"复盘 · {ev['seat_no']}号（{_ROLE_LABEL.get(ev['role'], ev['role'])}）"
        self.typist.panel(self.console, body, title=title, border_style=_SIDE_STYLE.get(ev["side"], "white"))

    def _on_host(self, ev: dict[str, Any]) -> None:
        """主持人台词：一行带话筒的旁白。文本与玩家线程里的公共叙述同源，
        结构化事件已各自渲染过细节，这里压成 dim 免得喧宾夺主。"""
        self.console.print(Text(f"🎙 {ev['text']}", style="dim cyan"))

    def _on_unknown(self, ev: dict[str, Any]) -> None:
        self.console.print(f"[dim]· {ev}[/dim]")
