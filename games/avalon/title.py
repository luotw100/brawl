"""`brawl avalon-title <replay_dir>`：为一局阿瓦隆 replay 生成发布文案。

这是 replay 消费端：只读一局已经冻好的 events.jsonl + meta.json，不碰对局逻辑。
产物写回同一个资源包目录：<replay_dir>/title.txt，内容包含标题、简介、标签。

标题是面向短视频发布的人类文案，必须接 LLM：规则层只负责把事件流压成事实摘要，
模型负责从戏剧冲突里提炼一个短短的吸睛标题与简介。输出仍走 JSON 解析校验，避免自由文本
格式飘掉。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, ClassVar

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm

from avalon.parsing import parse_json
from avalon.registry import ModelSpec, Registry, load_registry

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class SettingsError(Exception):
    """面向用户的错误（目录不对/模型未知/凭证缺失）：cli 入口只打印消息、不甩 traceback。"""


class TitleSettings(BaseSettings):
    """`brawl avalon-title` 的参数面（settings 即 CLI，flag 自动派生）。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_AVALON_TITLE_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    dir: CliPositionalArg[Path]  # replay 目录（含 events.jsonl / meta.json）
    model: str = "oc-deepseek-v4-flash"  # 用 registry 里的逻辑模型名生成发布文案

    @model_validator(mode="after")
    def _check(self) -> TitleSettings:
        if not self.dir.is_dir():
            raise ValueError(f"replay 目录不存在：{self.dir}")
        for name in ("events.jsonl", "meta.json"):
            if not (self.dir / name).is_file():
                raise ValueError(f"{self.dir} 不是 replay 目录（缺 {name}）")
        return self

    @property
    def out_path(self) -> Path:
        return self.dir / "title.txt"

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
                cli_prog_name="brawl avalon-title",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> TitleSettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None


class PublishCopy(BaseModel):
    """LLM 必须产出的发布文案。"""

    title: str = Field(min_length=6, max_length=36)
    intro: str = Field(min_length=10, max_length=120)
    tags: list[str] = Field(min_length=3, max_length=8)

    @field_validator("title", "intro")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        tags = []
        seen = set()
        for tag in value:
            clean = tag.strip().lstrip("#＃").replace(" ", "")
            if clean and clean not in seen:
                tags.append(clean)
                seen.add(clean)
        if len(tags) < 3:
            raise ValueError("tags 至少需要 3 个有效标签")
        return tags


@dataclass(frozen=True)
class TitlePlan:
    settings: TitleSettings
    model: ModelSpec


_ROLE_ZH = {
    "merlin": "梅林",
    "percival": "派西维尔",
    "servant": "忠臣",
    "assassin": "刺客",
    "morgana": "莫甘娜",
    "mordred": "莫德雷德",
    "oberon": "奥伯伦",
    "minion": "爪牙",
}

_RESULT_ZH = {"success": "成功", "fail": "失败"}
_WINNER_ZH = {"good": "蓝方", "evil": "红方"}


def resolve_and_validate(settings: TitleSettings, registry: Registry) -> TitlePlan:
    """标题生成的边界：模型引用与凭证一次性查清，下游直接调用。"""
    model = registry.models.get(settings.model)
    if model is None:
        raise SettingsError(f"未知标题模型：{settings.model}（可选：{', '.join(registry.model_names())}）")
    if not os.environ.get(model.api_key_env):
        raise SettingsError(f"缺少 {model.api_key_env}，无法调用标题模型 {settings.model}")
    return TitlePlan(settings=settings, model=model)


def _read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _snip(text: str, n: int = 52) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _names(setup: dict[str, Any]) -> dict[int, str]:
    return {i + 1: name for i, name in enumerate(setup.get("names") or [])}


def _roles(setup: dict[str, Any]) -> str:
    roles = [_ROLE_ZH.get(r, r) for r in setup.get("roles") or []]
    if not roles:
        return "未知阵容"
    counts: dict[str, int] = {}
    for role in roles:
        counts[role] = counts.get(role, 0) + 1
    return "、".join(f"{role}×{count}" if count > 1 else role for role, count in counts.items())


def build_brief(events: list[dict[str, Any]], setup: dict[str, Any]) -> str:
    """把完整事件流压成给标题模型看的事实摘要。只取公开关键转折，避免把整局塞进上下文。"""
    names = _names(setup)
    lines = [
        f"人数：{setup.get('players') or len(setup.get('roles') or [])} 人",
        f"阵容：{_roles(setup)}",
        f"湖中仙女：{'启用' if setup.get('lady_holder') is not None else '未启用'}",
    ]
    for ev in events:
        t = ev.get("type")
        if t == "propose":
            leader = names.get(ev["leader_no"], f"{ev['leader_no']}号")
            lines.append(f"任务{ev['quest']}.{ev['attempt']}：{leader} 提队 {ev['team']}。发言：{_snip(ev.get('speech') or '')}")
        elif t == "vote_result":
            outcome = "通过" if ev["approved"] else "否决"
            lines.append(f"任务{ev['quest']}.{ev['attempt']} 表决{outcome}：{ev['approves']} 赞成 / {ev['rejections']} 反对")
        elif t == "quest_result":
            result = _RESULT_ZH.get(ev["result"], ev["result"])
            lines.append(f"任务{ev['quest']} {result}：队伍 {ev['team']}，破坏票 {ev['fails']}，比分 蓝{ev['succeeded']} : 红{ev['failed']}")
        elif t == "lady":
            holder = names.get(ev["holder_no"], f"{ev['holder_no']}号")
            target = names.get(ev["target_no"], f"{ev['target_no']}号")
            side = "蓝方" if ev.get("truth") == "good" else "红方"
            lines.append(f"仙女：{holder} 查验 {target}，真实阵营 {side}。公开说法：{_snip(ev.get('speech') or '')}")
        elif t == "assassination":
            assassin = names.get(ev["assassin_no"], f"{ev['assassin_no']}号")
            target = names.get(ev["target_no"], f"{ev['target_no']}号")
            hit = "命中" if ev.get("hit") else "落空"
            lines.append(f"刺杀：{assassin} 刺杀 {target}，结果{hit}。发言：{_snip(ev.get('speech') or '')}")
        elif t == "game_over":
            winner = _WINNER_ZH.get(ev.get("winner"), ev.get("winner", "未知"))
            lines.append(f"终局：{winner}获胜，原因：{ev.get('win_reason') or ev.get('reason') or '未知'}")
    return "\n".join(lines[-70:])


def _prompt(brief: str) -> list:
    system = (
        "你是短视频发布编辑，擅长把一局阿瓦隆桌游剪辑包装成吸睛但不造假的标题。"
        "只基于给定事实写文案，不要编造不存在的反转。"
    )
    human = f"""
请为下面这局阿瓦隆生成发布文案。要求：
- 标题：中文，6~36字，短、抓人、有具体冲突点，不要标题党造假。
- 简介：中文，10~120字，说明这局最值得看的反转/误判/刺杀/仙女信息。
- 标签：3~8个中文标签，不带 #。
- 只输出 JSON 对象，字段严格为 title、intro、tags。

事实摘要：
{brief}
""".strip()
    return [SystemMessage(system), HumanMessage(human)]


def format_copy(copy: PublishCopy) -> str:
    tags = " ".join(f"#{tag}" for tag in copy.tags)
    return f"标题：{copy.title}\n简介：{copy.intro}\n标签：{tags}\n"


def _model(spec: ModelSpec) -> ChatOpenAI:
    return ChatOpenAI(
        model=spec.model_id,
        base_url=spec.base_url,
        api_key=os.environ[spec.api_key_env],
        temperature=spec.temperature,
        max_tokens=spec.max_tokens,
        timeout=spec.timeout,
        extra_body=spec.extra_body or None,
    )


def _confirm_overwrite(path: Path, console: Console) -> bool:
    if not path.exists():
        return True
    console.print(Panel.fit(
        f"[bold yellow]{path.name} 已存在[/bold yellow]\n\n[dim]{path}[/dim]\n\n继续生成会覆盖这份发布文案。",
        title="确认覆盖",
        border_style="yellow",
    ))
    return Confirm.ask("要覆盖并重新生成标题文案吗？", default=False, console=console)


async def _run(plan: TitlePlan, console: Console) -> None:
    settings = plan.settings
    meta = json.loads((settings.dir / "meta.json").read_text(encoding="utf-8"))["setup"]
    events = _read_events(settings.dir / "events.jsonl")
    brief = build_brief(events, meta)
    messages = _prompt(brief)

    console.print(f"[bold]生成发布文案[/bold] [dim]{settings.dir.name} · {plan.model.name}[/dim]")
    t0 = perf_counter()
    resp = await _model(plan.model).ainvoke(messages)
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    copy = parse_json(text, PublishCopy)
    settings.out_path.write_text(format_copy(copy), encoding="utf-8")
    console.print(f"[bold green]✓ 已生成[/bold green] {settings.out_path} [dim]({perf_counter() - t0:.1f}s)[/dim]")


def main(argv: list[str]) -> None:
    """`brawl avalon-title` 的薄入口：argv 全交给 TitleSettings 的 CLI source。"""
    console = Console()
    settings = TitleSettings.load(cli_args=argv)
    if not _confirm_overwrite(settings.out_path, console):
        console.print("[yellow]已取消生成，原文案保留。[/yellow]")
        return
    asyncio.run(_run(resolve_and_validate(settings, load_registry()), console))


__all__ = ["TitleSettings", "SettingsError", "PublishCopy", "build_brief", "format_copy", "main"]
