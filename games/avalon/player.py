"""Avalon 玩家决策层。

跨玩法的 agent 调用、重试与审计在 core.actor；本文件只保留 Avalon 专属的
PlayerView、Player 协议、RandomAgent（本地模拟）与八个决策动作。
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from avalon import prompts
from avalon.registry import ModelSpec, Registry
from avalon.settings import AvalonSettings
from avalon.state import Assassination, LadyChoice, Proposal, ProposalRecord, QuestCard, Role, Side, Statement, TeamVote
from core.actor import AgentActor, ModelBackend, OpenCodeBackend
from core.parsing import JsonParseError, parse_json
from core.recorder import Recorder
from core.threads import ThreadMessage


@dataclass
class PlayerView:
    """喂给 Player 的、某席位当前能看到的全部信息。座位号一律 1..players。"""

    seat_no: int
    role: Role
    intel: list[int]
    model: ModelSpec | None
    lineup: list[Role]
    players: int
    leader_no: int
    quest_no: int
    attempt: int
    team_size: int
    fails_required: int
    succeeded: int
    failed: int
    team: list[int]
    lady_used: list[int]
    lady_target: int | None = None
    lady_seen: str | None = None
    tone: str = ""
    strategy: str = ""
    history: list[ProposalRecord] = field(default_factory=list)
    messages: list[ThreadMessage] = field(default_factory=list)
    prompt: str = ""

    def others(self) -> list[int]:
        return [no for no in range(1, self.players + 1) if no != self.seat_no]

    def good_suspects(self) -> list[int]:
        """除自己和已知同伙外的座位：红方视角下可能是梅林的候选。"""
        return [no for no in self.others() if no not in self.intel]

    def lady_candidates(self) -> list[int]:
        """湖中仙女可查验的对象：从未持有过令牌的人。"""
        return [no for no in range(1, self.players + 1) if no not in self.lady_used]


class Player(Protocol):
    async def close(self) -> None: ...
    async def propose(self, view: PlayerView) -> tuple[Proposal, str]: ...
    async def discuss(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def vote_team(self, view: PlayerView) -> tuple[TeamVote, str]: ...
    async def play_quest(self, view: PlayerView) -> tuple[QuestCard, str]: ...
    async def assassinate(self, view: PlayerView) -> tuple[Assassination, str]: ...
    async def lady_choose(self, view: PlayerView) -> tuple[LadyChoice, str]: ...
    async def lady_announce(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def banter(self, view: PlayerView) -> tuple[Statement, str]: ...


class RandomAgent:
    """本地随机 agent：不联网，按 PlayerView 现编合法 JSON 决策。"""

    def __init__(self, rng: random.Random) -> None:
        self._salt = rng.getrandbits(64)

    async def call(self, phase: str, view: PlayerView, spec: ModelSpec, messages: list[BaseMessage]) -> AIMessage:
        base = phase.removesuffix(".extract")
        decision = self._decide(base, view)
        return AIMessage(content=decision.model_dump_json())

    def _decide(self, base: str, view: PlayerView) -> BaseModel:
        r = random.Random(f"{self._salt}:{view.seat_no}:{view.quest_no}:{view.attempt}:{base}")
        if base == "propose":
            team = sorted(r.sample(range(1, view.players + 1), view.team_size))
            return Proposal(team=team, speech=f"（随机）我看 {'、'.join(f'{no}号' for no in team)} 就挺合适，出发。", monologue="（随机）闭眼点的将。")
        if base == "discuss":
            return Statement(speech="（随机）这队我说不上好坏，先看看大家的票。", monologue="（随机）水一句，别看我。")
        if base == "team_vote":
            return TeamVote(approve=r.random() < 0.5, monologue="（随机）抛硬币定的。")
        if base == "quest":
            success = True if view.role.side is Side.GOOD else r.random() < 0.5
            return QuestCard(success=success, monologue="（随机）牌已扣下，面无表情。")
        if base == "assassinate":
            target = r.choice(view.good_suspects())
            return Assassination(target_no=target, speech=f"（随机）直觉告诉我，{target}号就是梅林。", monologue="（随机）蒙的，但要蒙得自信。")
        if base == "lady_choose":
            return LadyChoice(target_no=r.choice(view.lady_candidates()), monologue="（随机）就查你了。")
        if base == "lady_announce":
            claim = view.lady_seen if r.random() < 0.5 else ("evil" if view.lady_seen == "good" else "good")
            side = "蓝方" if claim == "good" else "红方"
            return Statement(speech=f"（随机）我查验了{view.lady_target}号——{side}。", monologue="（随机）信不信由你们。")
        if base == "banter":
            return Statement(speech="（随机）这局打完了，各位辛苦，下把再战。", monologue="（随机）复盘就免了，赢牌全靠脸。")
        raise ValueError(f"未知决策阶段：{base}")


class AgentPlayer(AgentActor):
    """Avalon agent 玩家：通过 agent 后端获取 JSON 决策并解析。"""

    async def _say(self, phase: str, view: PlayerView) -> str:
        history = [HumanMessage(m.content) if m.role == "human" else AIMessage(m.content) for m in view.messages]
        messages: list[BaseMessage] = [SystemMessage(prompts.system_text(view)), *history, HumanMessage(view.prompt)]
        return await self._invoke(phase, view, view.model, messages)

    async def _parse[T: BaseModel](self, raw: str, model_cls: type[T]) -> T:
        try:
            return parse_json(raw, model_cls)
        except JsonParseError as e:
            raise JsonParseError(f"Avalon JSON 解析失败：{e}") from e

    async def propose(self, view: PlayerView) -> tuple[Proposal, str]:
        async def once() -> tuple[Proposal, str]:
            raw = await self._say("propose", view)
            return await self._parse(raw, Proposal), raw
        return await self._act("propose", view, once)

    async def discuss(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("discuss", view)
            return await self._parse(raw, Statement), raw
        return await self._act("discuss", view, once)

    async def vote_team(self, view: PlayerView) -> tuple[TeamVote, str]:
        async def once() -> tuple[TeamVote, str]:
            raw = await self._say("team_vote", view)
            return await self._parse(raw, TeamVote), raw
        return await self._act("team_vote", view, once)

    async def play_quest(self, view: PlayerView) -> tuple[QuestCard, str]:
        async def once() -> tuple[QuestCard, str]:
            raw = await self._say("quest", view)
            return await self._parse(raw, QuestCard), raw
        return await self._act("quest", view, once)

    async def assassinate(self, view: PlayerView) -> tuple[Assassination, str]:
        async def once() -> tuple[Assassination, str]:
            raw = await self._say("assassinate", view)
            return await self._parse(raw, Assassination), raw
        return await self._act("assassinate", view, once)

    async def lady_choose(self, view: PlayerView) -> tuple[LadyChoice, str]:
        async def once() -> tuple[LadyChoice, str]:
            raw = await self._say("lady_choose", view)
            return await self._parse(raw, LadyChoice), raw
        return await self._act("lady_choose", view, once)

    async def lady_announce(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("lady_announce", view)
            return await self._parse(raw, Statement), raw
        return await self._act("lady_announce", view, once)

    async def banter(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("banter", view)
            return await self._parse(raw, Statement), raw
        return await self._act("banter", view, once)


def build_player(
    settings: AvalonSettings,
    rng: random.Random,
    recorder: Recorder,
    registry: Registry | None = None,
    models: list[str] | None = None,
) -> Player:
    """边界工厂：random 注入 RandomAgent，opencode 注入 OpenCodeBackend。

    opencode 模式下，`models` 是每座位逻辑模型名（已解析好的），registry 里若该模型配有
    `opencode_model`（providerID/modelID），则按座位映射到对应 OpenCode 模型；否则用
    `--opencode-model` 默认值或 server 默认模型。
    """
    kind = settings.player
    if kind == "random":
        backend: ModelBackend = RandomAgent(rng)
    elif kind == "opencode":
        seat_models: dict[int, str] = {}
        logical = models or settings.models or []
        for i, name in enumerate(logical, start=1):
            ref: str | None = None
            if registry is not None and name in registry.models:
                ref = registry.models[name].opencode_model
            if ref:
                seat_models[i] = ref
        backend = OpenCodeBackend(
            base_url=settings.opencode_url,
            username=settings.opencode_username,
            password=os.environ.get(settings.opencode_password_env),
            default_model=settings.opencode_model,
            seat_models=seat_models,
        )
    else:
        raise ValueError(f"不支持的 player 类型：{kind}")
    return AgentPlayer(recorder=recorder, backend=backend)
