"""狼人杀玩家决策层。"""

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
        return [no for no in self.alive_others() if no not in self.wolf_allies]


class Player(Protocol):
    async def wolf_kill(self, view: PlayerView) -> tuple[WolfKill, str]: ...
    async def seer_check(self, view: PlayerView) -> tuple[SeerAction, str]: ...
    async def witch_action(self, view: PlayerView) -> tuple[WitchAction, str]: ...
    async def last_words(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def discuss(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def vote(self, view: PlayerView) -> tuple[VoteAction, str]: ...
    async def hunter_shoot(self, view: PlayerView) -> tuple[HunterShot, str]: ...


class FakeBackend:
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
            target = r.choice(view.wolf_targets())
            return WolfKill(target_no=target, speech=f"（随机）我建议刀{target}号。", monologue="（随机）先找个看起来像神的。")
        if base == "seer_check":
            target = r.choice(view.alive_others())
            return SeerAction(target_no=target, monologue="（随机）验一个顺眼的。")
        if base == "witch_action":
            poison_candidates = view.alive_others()
            save = view.witch_has_antidote and view.night_target is not None and r.random() < 0.35
            poison = None
            if view.witch_has_poison and r.random() < 0.25 and poison_candidates:
                poison = r.choice(poison_candidates)
            if poison == view.night_target and save:
                poison = None
            return WitchAction(save=save, poison_target_no=poison, monologue="（随机）药瓶凭感觉用。")
        if base == "vote":
            target = r.choice(view.alive_others())
            return VoteAction(target_no=target, monologue="（随机）票一个压力位。")
        if base == "hunter_shoot":
            target = r.choice(view.alive_others()) if r.random() < 0.8 else None
            text = f"（随机）我开枪带走{target}号。" if target is not None else "（随机）我不开枪。"
            return HunterShot(target_no=target, speech=text, monologue="（随机）临走前再赌一次。")
        if base in {"discuss", "last_words"}:
            return Statement(speech="（随机）我先按当前票型和发言站边，后面看结果修正。", monologue="（随机）别太用力，避免背锅。")
        raise ValueError(f"未知决策阶段：{base}")


def _render(d: BaseModel) -> str:
    if isinstance(d, WolfKill):
        return f"内心戏：{d.monologue}\n狼队发言：{d.speech}\n刀人：{d.target_no}"
    if isinstance(d, SeerAction):
        return f"内心戏：{d.monologue}\n查验：{d.target_no}"
    if isinstance(d, WitchAction):
        return f"内心戏：{d.monologue}\n救人：{d.save}\n毒人：{d.poison_target_no}"
    if isinstance(d, VoteAction):
        return f"内心戏：{d.monologue}\n投票：{d.target_no}"
    if isinstance(d, HunterShot):
        return f"内心戏：{d.monologue}\n发言：{d.speech}\n开枪：{d.target_no}"
    assert isinstance(d, Statement)
    return f"内心戏：{d.monologue}\n发言：{d.speech}"


class LLMPlayer(LLMActor):
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
            return await self._read(
                "witch_action",
                view,
                prompts.extract_witch(raw, view.alive_others(), view.witch_has_antidote and view.night_target is not None, view.witch_has_poison),
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


def build_player(kind: Literal["llm", "random"], rng: random.Random, recorder: Recorder, extractor: ModelSpec) -> Player:
    backend: ModelBackend = FakeBackend(rng) if kind == "random" else LiveBackend()
    return LLMPlayer(recorder=recorder, extractor=extractor, backend=backend)

