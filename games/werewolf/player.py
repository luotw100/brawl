"""狼人杀玩家决策层。

跨玩法的模型调用、重试、extractor 解析与 llm_calls 落盘在 core.actor；本文件只保留狼人杀
专属的 PlayerView、Player 协议、离线 FakeBackend 与七个决策动作。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from core.actor import LiveBackend, LLMActor, ModelBackend
from core.recorder import Recorder
from core.threads import ThreadMessage
from werewolf import prompts
from werewolf.registry import ModelSpec
from werewolf.state import HunterShot, Role, SeerAction, SeerCheck, Statement, VoteAction, WitchAction, WolfKill


@dataclass
class PlayerView:
    """喂给 Player 的、某席位当前能看到的全部信息。座位号一律 1..players。

    quest_no/attempt 是给 core.actor 落 llm_call call_id 用的对齐键：狼人杀一天一回合、
    无重提概念，故 quest_no=当前天、attempt 恒为 1。"""

    seat_no: int
    role: Role
    model: ModelSpec
    players: int
    day: int
    alive_nos: list[int]
    dead_nos: list[int]
    wolf_allies: list[int]
    seer_checks: list[SeerCheck]
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
    async def wolf_kill(self, view: PlayerView) -> tuple[WolfKill, str]: ...
    async def seer_check(self, view: PlayerView) -> tuple[SeerAction, str]: ...
    async def witch_action(self, view: PlayerView) -> tuple[WitchAction, str]: ...
    async def last_words(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def discuss(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def vote(self, view: PlayerView) -> tuple[VoteAction, str]: ...
    async def hunter_shoot(self, view: PlayerView) -> tuple[HunterShot, str]: ...
    async def banter(self, view: PlayerView) -> tuple[Statement, str]: ...


class FakeBackend:
    """离线假后端：不联网、不读 key，按 PlayerView 现编合法自由文本/JSON。"""

    def __init__(self, rng: random.Random) -> None:
        self._salt = rng.getrandbits(64)

    async def call(self, phase: str, view: PlayerView, spec: ModelSpec, messages: list[BaseMessage]) -> BaseMessage:
        base = phase.removesuffix(".extract")
        decision = self._decide(base, view)
        content = decision.model_dump_json() if phase.endswith(".extract") else _render(decision)
        return AIMessage(content=content)

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


def _render(d: BaseModel) -> str:
    """把假后端的结构化决策渲染回标签行自由文本。"""
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


class LLMPlayer(LLMActor):
    """狼人杀决策动作；通用 LLM 链路由 LLMActor 提供。"""

    def __init__(self, recorder: Recorder, extractor: ModelSpec, backend: ModelBackend) -> None:
        super().__init__(recorder=recorder, extractor=extractor, backend=backend)

    async def _say(self, phase: str, view: PlayerView) -> str:
        return await self._converse(phase, view, prompts.system_text(view))

    async def _read[T: BaseModel](self, phase: str, view: PlayerView, instruction: str, model_cls: type[T]) -> T:
        return await self._extract(phase, view, prompts.EXTRACT_SYSTEM, instruction, model_cls)

    async def wolf_kill(self, view: PlayerView) -> tuple[WolfKill, str]:
        async def once() -> tuple[WolfKill, str]:
            raw = await self._say("wolf_kill", view)
            return await self._read("wolf_kill", view, prompts.extract_wolf_kill(raw, view.wolf_targets()), WolfKill), raw

        return await self._act("wolf_kill", view, once)

    async def seer_check(self, view: PlayerView) -> tuple[SeerAction, str]:
        async def once() -> tuple[SeerAction, str]:
            raw = await self._say("seer_check", view)
            return await self._read("seer_check", view, prompts.extract_seer(raw, view.alive_others()), SeerAction), raw

        return await self._act("seer_check", view, once)

    async def witch_action(self, view: PlayerView) -> tuple[WitchAction, str]:
        async def once() -> tuple[WitchAction, str]:
            raw = await self._say("witch_action", view)
            can_save = view.witch_has_antidote and view.night_target is not None
            return await self._read(
                "witch_action", view,
                prompts.extract_witch(raw, view.alive_others(), can_save, view.witch_has_poison),
                WitchAction,
            ), raw

        return await self._act("witch_action", view, once)

    async def last_words(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("last_words", view)
            return await self._read("last_words", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("last_words", view, once)

    async def discuss(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("discuss", view)
            return await self._read("discuss", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("discuss", view, once)

    async def vote(self, view: PlayerView) -> tuple[VoteAction, str]:
        async def once() -> tuple[VoteAction, str]:
            raw = await self._say("vote", view)
            return await self._read("vote", view, prompts.extract_vote(raw, view.alive_others()), VoteAction), raw

        return await self._act("vote", view, once)

    async def hunter_shoot(self, view: PlayerView) -> tuple[HunterShot, str]:
        async def once() -> tuple[HunterShot, str]:
            raw = await self._say("hunter_shoot", view)
            return await self._read("hunter_shoot", view, prompts.extract_hunter(raw, view.alive_others()), HunterShot), raw

        return await self._act("hunter_shoot", view, once)

    async def banter(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("banter", view)
            return await self._read("banter", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("banter", view, once)


def build_player(kind: Literal["llm", "random"], rng: random.Random, recorder: Recorder, extractor: ModelSpec) -> Player:
    """边界工厂：llm 注入 LiveBackend，random 注入狼人杀 FakeBackend。"""
    backend: ModelBackend = FakeBackend(rng) if kind == "random" else LiveBackend()
    return LLMPlayer(recorder=recorder, extractor=extractor, backend=backend)
