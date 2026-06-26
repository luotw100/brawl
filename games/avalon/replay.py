"""`brawl avalon-replay <dir>`：回放一局 replay 快照，像 log 一样把对局过程打到终端。

games 是"对局快照生成器"：跑一遍把一局冻成 replay（events.jsonl + llm_calls.jsonl +
meta.json）。本模块是**回放消费端**——只读 events.jsonl、喂给现成的 console.Renderer，
节奏（逐字打字机 + 事件间停顿）全在这一层。生成器永远零 wall-clock，sleep 只属于这里。

  pace      0~1 节奏旋钮：0 = 瞬间倾倒（调试/翻档），1 = 慢慢打字朗读。台词逐字长出，
            停留时长由文本长度自然浮现（"按字数模拟阅读"无需另算）。
  show_cost 旁注每次 LLM 调用的 token 与耗时（主调用 / extract 抽取分开列），末尾给总账。
            靠 events 与 llm_calls 共享的 call_id 严格对齐（events.decision_id）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from rich.console import Console
from rich.table import Table
from rich.text import Text

from avalon.console import InstantTypist, PacedTypist, Renderer
from core.events import decision_id

# pace=1 时的上限：每字停顿 / 事件间停顿（秒）。pace 在 0..1 间线性缩放到这两个上限。
_MAX_CHAR_DELAY = 0.06
_MAX_BEAT = 0.7


class ReplaySettings(BaseSettings):
    """`brawl avalon-replay` 的参数面（settings 即 CLI，flag 自动派生）。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_AVALON_REPLAY_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    dir: CliPositionalArg[Path]  # replay 目录（含 events.jsonl / llm_calls.jsonl / meta.json）
    pace: float = 0.5  # 0~1 节奏旋钮
    show_cost: bool = False  # 旁注每次 LLM 调用的 token 与耗时

    @model_validator(mode="after")
    def _check(self) -> ReplaySettings:
        if not 0.0 <= self.pace <= 1.0:
            raise ValueError(f"pace 必须在 0~1 之间，收到 {self.pace}")
        if not self.dir.is_dir():
            raise ValueError(f"replay 目录不存在：{self.dir}")
        if not (self.dir / "events.jsonl").is_file():
            raise ValueError(f"{self.dir} 不是 replay 目录（缺 events.jsonl）")
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
            init_settings,  # 直接 kwargs 最高（代码/测试照常生效）
            CliSettingsSource(
                settings_cls,
                cli_parse_args=cls._cli_args if cls._cli_args is not None else [],
                cli_prog_name="brawl avalon-replay",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> ReplaySettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None


# ---------------------------------------------------------------- 读 replay


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 成本对齐与渲染


def _index_calls(records: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """按 call_id 把 llm_calls 归位，每个决策下分 main / extract 两格。

    call_id 优先用记录里存的；老 replay 没存就由 (quest,attempt,phase,seat) 现算——
    与生成端 decision_id 同一公式，故新老 replay 一视同仁。"""
    idx: dict[str, dict[str, dict[str, Any]]] = {}
    for r in records:
        cid = r.get("call_id") or decision_id(r["quest"], r["attempt"], r["phase"], r["seat_no"])
        slot = idx.setdefault(cid, {})
        slot["extract" if str(r["phase"]).endswith(".extract") else "main"] = r
    return idx


def _event_calls(ev: dict[str, Any]) -> list[tuple[str, str | None]]:
    """一个事件背后的决策 (标签, call_id) 列表。老 replay 缺 call_id 时返回 None，跳过旁注。"""
    t = ev["type"]
    if t == "propose":
        return [(f"{ev['leader_no']}号 提案", ev.get("call_id"))]
    if t == "discuss":
        return [(f"{ev['seat_no']}号 发言", ev.get("call_id"))]
    if t == "vote_result":
        return [(f"{v['seat_no']}号 表决", v.get("call_id")) for v in ev["votes"]]
    if t == "quest_result":
        return [(f"{c['seat_no']}号 任务", c.get("call_id")) for c in ev["cards"]]
    if t == "lady":
        return [(f"{ev['holder_no']}号 查验", ev.get("choose_call_id")), (f"{ev['holder_no']}号 宣布", ev.get("announce_call_id"))]
    if t == "assassination":
        return [(f"{ev['assassin_no']}号 刺杀", ev.get("call_id"))]
    if t == "banter":
        return [(f"{ev['seat_no']}号 复盘", ev.get("call_id"))]
    return []


def _call_load(rec: dict[str, Any] | None) -> tuple[int, int]:
    """一次调用的 (总 token, 耗时 ms)；缺记录/缺用量记 0。"""
    if rec is None:
        return 0, 0
    u = rec.get("usage") or {}
    total = u.get("total_tokens") or ((u.get("input_tokens") or 0) + (u.get("output_tokens") or 0))
    return total, rec.get("latency_ms") or 0


def _print_cost(console: Console, ev: dict[str, Any], idx: dict[str, dict[str, dict[str, Any]]]) -> None:
    """发言/投票后瞬间旁注这次决策的调用量(主+extract 合计 token 与耗时)。"""
    for label, cid in _event_calls(ev):
        if cid is None:
            continue
        slot = idx.get(cid, {})
        mt, mms = _call_load(slot.get("main"))
        et, ems = _call_load(slot.get("extract"))
        line = Text(f"  ↳ {label} 调用量 ", style="dim")
        line.append(f"{mt + et:,} tok / {(mms + ems) / 1000:.1f}s", style="dim yellow")
        line.append(f"（主 {mt:,} · 抽取 {et:,}）", style="dim")
        console.print(line)


def _model_table(title: str, records: list[dict[str, Any]]) -> Table:
    """一类调用(主 / extract)的分模型账：每模型一行 + 末尾汇总。token 列右对齐。"""
    agg: dict[str, dict[str, int]] = {}
    for r in records:
        b = agg.setdefault(r["model"], {"n": 0, "in": 0, "out": 0, "cache": 0, "ms": 0})
        u = r.get("usage") or {}
        b["n"] += 1
        b["in"] += u.get("input_tokens", 0) or 0
        b["out"] += u.get("output_tokens", 0) or 0
        b["cache"] += (u.get("input_token_details") or {}).get("cache_read", 0) or 0
        b["ms"] += r.get("latency_ms", 0) or 0

    table = Table(title=title)
    table.add_column("模型")
    for col in ("次数", "输入tok", "输出tok", "缓存命中", "总耗时", "均耗时"):
        table.add_column(col, justify="right")

    def _row(name: str, b: dict[str, int], style: str | None = None) -> None:
        avg = b["ms"] / b["n"] if b["n"] else 0
        table.add_row(name, f"{b['n']:,}", f"{b['in']:,}", f"{b['out']:,}", f"{b['cache']:,}", f"{b['ms'] / 1000:.1f}s", f"{avg:.0f}ms", style=style)

    total = {"n": 0, "in": 0, "out": 0, "cache": 0, "ms": 0}
    for model in sorted(agg):
        b = agg[model]
        _row(model, b)
        for k in total:
            total[k] += b[k]
    table.add_section()
    _row("汇总", total, style="bold")
    return table


def _print_summary(console: Console, records: list[dict[str, Any]]) -> None:
    """末尾总账：主调用、extract 抽取调用各一张分模型表。"""
    mains = [r for r in records if not str(r["phase"]).endswith(".extract")]
    extracts = [r for r in records if str(r["phase"]).endswith(".extract")]
    console.print(_model_table("主调用 · 分模型", mains))
    console.print(_model_table("extract 抽取调用 · 分模型", extracts))


# ---------------------------------------------------------------- 驱动


def run_replay(settings: ReplaySettings) -> None:
    events = _load_jsonl(settings.dir / "events.jsonl")
    llm_path = settings.dir / "llm_calls.jsonl"
    calls = _load_jsonl(llm_path) if (settings.show_cost and llm_path.is_file()) else []
    idx = _index_calls(calls) if calls else {}

    typist = PacedTypist(settings.pace * _MAX_CHAR_DELAY) if settings.pace > 0 else InstantTypist()
    renderer = Renderer(typist=typist)
    console = renderer.console
    beat = settings.pace * _MAX_BEAT

    for ev in events:
        renderer.handle(ev)
        if settings.show_cost:
            _print_cost(console, ev, idx)
        if beat:
            time.sleep(beat)

    if calls:
        _print_summary(console, calls)


def main(argv: list[str]) -> None:
    """`brawl avalon-replay` 的薄入口：argv 全交给 ReplaySettings 的 CLI source。"""
    run_replay(ReplaySettings.load(cli_args=argv))
