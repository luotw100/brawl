"""狼人杀玩家决策层。

跨玩法的 agent 调用、重试与审计在 core.actor；本文件只保留狼人杀专属的
PlayerView、Player 协议、RandomAgent（本地模拟）与七个决策动作。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from core.actor import AgentActor, ModelBackend, OpenCodeBackend
from core.opencode_runtime import OpenCodeConnection
from core.parsing import JsonParseError
from core.recorder import Recorder
from core.threads import ThreadMessage
from werewolf import prompts
from werewolf.registry import ModelSpec, Registry
from werewolf.settings import WerewolfSettings
from werewolf.state import HunterShot, SeerAction, Statement, VoteAction, WitchAction, WolfKill


@dataclass
class PlayerView:
    """喂给 Player 的、某席位当前能看到的全部信息。座位号一律 1..players。"""

    seat_no: int
    role: object  # werewolf.rules.Role
    model: ModelSpec
    players: int
    day: int
    alive_nos: list[int]
    dead_nos: list[int]
    wolf_allies: list[int]
    seer_checks: list[object]
    witch_has_antidote: bool
    witch_has_poison: bool
    night_target: int | None
    tone: str = ""
    strategy: str = ""
    messages: list[ThreadMessage] = field(default_factory=list)
    prompt: str = ""

    @property
    def quest_no(self) -> int:
        return self.day

    @property
    def attempt(self) -> int:
        return 1

    def alive_others(self) -> list[int]:
        return [no for no in self.alive_nos if no != self.seat_no]

    def wolf_targets(self) -> list[int]:
        """狼人提刀的候选：场上存活的非狼队友（含其它角色，不含自己与同伴）。"""
        return [no for no in self.alive_others() if no not in self.wolf_allies]


class Player(Protocol):
    async def close(self) -> None: ...
    async def wolf_kill(self, view: PlayerView) -> tuple[WolfKill, str]: ...
    async def seer_check(self, view: PlayerView) -> tuple[SeerAction, str]: ...
    async def witch_action(self, view: PlayerView) -> tuple[WitchAction, str]: ...
    async def last_words(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def discuss(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def vote(self, view: PlayerView) -> tuple[VoteAction, str]: ...
    async def hunter_shoot(self, view: PlayerView) -> tuple[HunterShot, str]: ...
    async def banter(self, view: PlayerView) -> tuple[Statement, str]: ...


def _find_line(raw: str, prefix: str) -> str | None:
    """从多行文本中匹配「prefix：值」并返回最后一次出现的值（去首尾空白）。"""
    pattern = re.compile(rf"^{re.escape(prefix)}[\s：:]*(.*)$", re.MULTILINE)
    matches = pattern.findall(raw)
    return matches[-1].strip() if matches else None


def _parse_int(raw: str, prefix: str, candidates: list[int] | None = None) -> int:
    val = _find_line(raw, prefix)
    if val is None:
        raise JsonParseError(f"缺少「{prefix}」行")
    try:
        no = int(val)
    except ValueError as e:
        raise JsonParseError(f"「{prefix}」不是有效座位号：{val!r}") from e
    if candidates is not None and no not in candidates:
        raise JsonParseError(f"「{prefix}」{no} 不在候选列表 {candidates}")
    return no


def _parse_optional_int(raw: str, prefix: str, null_markers: set[str]) -> int | None:
    val = _find_line(raw, prefix)
    if val is None:
        raise JsonParseError(f"缺少「{prefix}」行")
    if val.casefold() in {m.casefold() for m in null_markers}:
        return None
    try:
        return int(val)
    except ValueError as e:
        raise JsonParseError(f"「{prefix}」不是有效座位号或{'/'.join(null_markers)}：{val!r}") from e


def _parse_statement(raw: str) -> Statement:
    speech = _find_line(raw, "发言")
    mono = _find_line(raw, "内心戏")
    if not speech:
        raise JsonParseError("缺少或为空「发言」行")
    if not mono:
        raise JsonParseError("缺少或为空「内心戏」行")
    return Statement(speech=speech, monologue=mono)


def _parse_wolf_kill(raw: str, view: PlayerView) -> WolfKill:
    speech = _find_line(raw, "发言")
    mono = _find_line(raw, "内心戏")
    target = _parse_int(raw, "刀人", view.wolf_targets())
    if not speech or not mono:
        raise JsonParseError("狼人夜聊/内心戏不能为空")
    return WolfKill(target_no=target, speech=speech, monologue=mono)


def _parse_seer(raw: str, view: PlayerView) -> SeerAction:
    mono = _find_line(raw, "内心戏")
    target = _parse_int(raw, "查验", view.alive_others())
    if not mono:
        raise JsonParseError("内心戏不能为空")
    return SeerAction(target_no=target, monologue=mono)


def _parse_witch(raw: str, _view: PlayerView) -> WitchAction:
    mono = _find_line(raw, "内心戏")
    save_text = _find_line(raw, "救人")
    if save_text is None:
        raise JsonParseError("缺少「救人」行")
    save_cf = save_text.casefold()
    if save_cf in {"是", "救", "yes", "true"}:
        save = True
    elif save_cf in {"否", "不", "no", "false"}:
        save = False
    else:
        raise JsonParseError(f"「救人」必须是 是/否/yes/no 之一，收到：{save_text!r}")
    poison = _parse_optional_int(raw, "用毒", {"不用", "无", "none", "null"})
    if not mono:
        raise JsonParseError("内心戏不能为空")
    return WitchAction(save=save, poison_target_no=poison, monologue=mono)


def _parse_vote(raw: str, view: PlayerView) -> VoteAction:
    mono = _find_line(raw, "内心戏")
    target = _parse_int(raw, "投票", view.alive_others())
    if not mono:
        raise JsonParseError("内心戏不能为空")
    return VoteAction(target_no=target, monologue=mono)


def _parse_hunter(raw: str, view: PlayerView) -> HunterShot:
    speech = _find_line(raw, "发言")
    mono = _find_line(raw, "内心戏")
    target = _parse_optional_int(raw, "开枪", {"不开枪", "不", "不用", "none", "null"})
    if not speech or not mono:
        raise JsonParseError("发言/内心戏不能为空")
    return HunterShot(target_no=target, speech=speech, monologue=mono)


class RandomAgent:
    """本地随机 agent：不联网，按 PlayerView 现编合法标签行决策。"""

    def __init__(self, rng: random.Random) -> None:
        self._salt = rng.getrandbits(64)

    async def call(self, phase: str, view: PlayerView, spec: ModelSpec, messages: list[BaseMessage]) -> AIMessage:
        base = phase.removesuffix(".extract")
        decision = self._decide(base, view)
        return AIMessage(content=_render(base, decision))

    def _decide(self, base: str, view: PlayerView) -> BaseModel:
        r = random.Random(f"{self._salt}:{view.seat_no}:{view.day}:{base}")
        if base == "wolf_kill":
            target = r.choice(view.wolf_targets() or view.alive_others())
            return WolfKill(target_no=target, speech=f"（随机）我建议刀{target}号，看着像神。", monologue="（随机）随手指一个。")
        if base == "seer_check":
            target = r.choice(view.alive_others())
            return SeerAction(target_no=target, monologue="（随机）验一个顺眼的。")
        if base == "witch_action":
            save = view.witch_has_antidote and view.night_target is not None and r.random() < 0.35
            poison = None
            if view.witch_has_poison and r.random() < 0.25 and view.alive_others():
                poison = r.choice(view.alive_others())
            if poison == view.night_target:
                poison = None
            return WitchAction(save=save, poison_target_no=poison, monologue="（随机）药瓶凭感觉用。")
        if base == "vote":
            target = r.choice(view.alive_others())
            return VoteAction(target_no=target, monologue="（随机）票一个压力位。")
        if base == "hunter_shoot":
            target = r.choice(view.alive_others()) if (r.random() < 0.8 and view.alive_others()) else None
            text = f"（随机）我开枪带走{target}号。" if target is not None else "（随机）我不开枪。"
            return HunterShot(target_no=target, speech=text, monologue="（随机）临走前再赌一次。")
        if base in {"discuss", "last_words", "banter"}:
            return Statement(speech="（随机）我先按当前票型和发言站边，后面看结果修正。", monologue="（随机）别太用力，避免背锅。")
        raise ValueError(f"未知决策阶段：{base}")


def _render(base: str, d: BaseModel) -> str:
    """把结构化决策渲染回标签行自由文本。"""
    if isinstance(d, WolfKill):
        return f"内心戏：{d.monologue}\n发言：{d.speech}\n刀人：{d.target_no}"
    if isinstance(d, SeerAction):
        return f"内心戏：{d.monologue}\n查验：{d.target_no}"
    if isinstance(d, WitchAction):
        poison = "不用" if d.poison_target_no is None else str(d.poison_target_no)
        return f"内心戏：{d.monologue}\n救人：{'是' if d.save else '否'}\n用毒：{poison}"
    if isinstance(d, VoteAction):
        return f"内心戏：{d.monologue}\n投票：{d.target_no}"
    if isinstance(d, HunterShot):
        shot = "不开枪" if d.target_no is None else str(d.target_no)
        return f"内心戏：{d.monologue}\n发言：{d.speech}\n开枪：{shot}"
    assert isinstance(d, Statement)
    return f"内心戏：{d.monologue}\n发言：{d.speech}"


class AgentPlayer(AgentActor):
    """狼人杀 agent 玩家：通过 agent 后端获取标签行决策并解析。"""

    async def _say(self, phase: str, view: PlayerView) -> str:
        history = [HumanMessage(m.content) if m.role == "human" else AIMessage(m.content) for m in view.messages]
        messages: list[BaseMessage] = [SystemMessage(prompts.system_text(view)), *history, HumanMessage(view.prompt)]
        return await self._invoke(phase, view, view.model, messages)

    async def _parse(self, phase: str, raw: str, view: PlayerView) -> BaseModel:
        try:
            if phase == "wolf_kill":
                return _parse_wolf_kill(raw, view)
            if phase == "seer_check":
                return _parse_seer(raw, view)
            if phase == "witch_action":
                return _parse_witch(raw, view)
            if phase in {"discuss", "last_words", "banter"}:
                return _parse_statement(raw)
            if phase == "vote":
                return _parse_vote(raw, view)
            if phase == "hunter_shoot":
                return _parse_hunter(raw, view)
        except Exception as e:
            raise JsonParseError(f"狼人杀解析失败（{phase}）：{e}") from e
        raise JsonParseError(f"未知决策阶段：{phase}")

    async def wolf_kill(self, view: PlayerView) -> tuple[WolfKill, str]:
        async def once() -> tuple[WolfKill, str]:
            raw = await self._say("wolf_kill", view)
            return await self._parse("wolf_kill", raw, view), raw  # type: ignore[return-value]
        return await self._act("wolf_kill", view, once)

    async def seer_check(self, view: PlayerView) -> tuple[SeerAction, str]:
        async def once() -> tuple[SeerAction, str]:
            raw = await self._say("seer_check", view)
            return await self._parse("seer_check", raw, view), raw  # type: ignore[return-value]
        return await self._act("seer_check", view, once)

    async def witch_action(self, view: PlayerView) -> tuple[WitchAction, str]:
        async def once() -> tuple[WitchAction, str]:
            raw = await self._say("witch_action", view)
            return await self._parse("witch_action", raw, view), raw  # type: ignore[return-value]
        return await self._act("witch_action", view, once)

    async def last_words(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("last_words", view)
            return await self._parse("last_words", raw, view), raw  # type: ignore[return-value]
        return await self._act("last_words", view, once)

    async def discuss(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("discuss", view)
            return await self._parse("discuss", raw, view), raw  # type: ignore[return-value]
        return await self._act("discuss", view, once)

    async def vote(self, view: PlayerView) -> tuple[VoteAction, str]:
        async def once() -> tuple[VoteAction, str]:
            raw = await self._say("vote", view)
            return await self._parse("vote", raw, view), raw  # type: ignore[return-value]
        return await self._act("vote", view, once)

    async def hunter_shoot(self, view: PlayerView) -> tuple[HunterShot, str]:
        async def once() -> tuple[HunterShot, str]:
            raw = await self._say("hunter_shoot", view)
            return await self._parse("hunter_shoot", raw, view), raw  # type: ignore[return-value]
        return await self._act("hunter_shoot", view, once)

    async def banter(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("banter", view)
            return await self._parse("banter", raw, view), raw  # type: ignore[return-value]
        return await self._act("banter", view, once)


def build_player(
    settings: WerewolfSettings,
    rng: random.Random,
    recorder: Recorder,
    registry: Registry | None = None,
    models: list[str] | None = None,
    connection: OpenCodeConnection | None = None,
) -> Player:
    """边界工厂：random 注入 RandomAgent，opencode 注入 OpenCodeBackend。"""
    kind = settings.player
    if kind == "random":
        backend: ModelBackend = RandomAgent(rng)
    elif kind == "opencode":
        assert connection is not None  # managed runtime 在进入此边界前已启动并探活
        seat_models: dict[int, str] = {}
        logical = models or settings.models or []
        for i, name in enumerate(logical, start=1):
            ref: str | None = None
            if registry is not None and name in registry.models:
                ref = registry.models[name].opencode_model
            if ref:
                seat_models[i] = ref
        backend = OpenCodeBackend(
            connection=connection,
            default_model=settings.opencode_model,
            seat_models=seat_models,
        )
    else:
        raise ValueError(f"不支持的 player 类型：{kind}")
    return AgentPlayer(recorder=recorder, backend=backend)
