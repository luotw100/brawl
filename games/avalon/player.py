"""Avalon 玩家决策层。

跨玩法的模型调用、重试、extractor 解析与 llm_calls 落盘在 core.actor；
本文件只保留 Avalon 专属的 PlayerView、Player 协议、离线 FakeBackend 与八个决策动作。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

from avalon import prompts
from avalon.registry import ModelSpec
from avalon.state import Assassination, LadyChoice, Proposal, ProposalRecord, QuestCard, Role, Side, Statement, TeamVote
from core.actor import LiveBackend, LLMActor, ModelBackend
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
    async def propose(self, view: PlayerView) -> tuple[Proposal, str]: ...
    async def discuss(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def vote_team(self, view: PlayerView) -> tuple[TeamVote, str]: ...
    async def play_quest(self, view: PlayerView) -> tuple[QuestCard, str]: ...
    async def assassinate(self, view: PlayerView) -> tuple[Assassination, str]: ...
    async def lady_choose(self, view: PlayerView) -> tuple[LadyChoice, str]: ...
    async def lady_announce(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def banter(self, view: PlayerView) -> tuple[Statement, str]: ...


class FakeBackend:
    """离线假后端：不联网、不读 key，按 PlayerView 现编合法自由文本/JSON。"""

    def __init__(self, rng: random.Random) -> None:
        self._salt = rng.getrandbits(64)

    async def call(self, phase: str, view: PlayerView, spec: ModelSpec, messages: list[BaseMessage]) -> BaseMessage:
        base = phase.removesuffix(".extract")
        decision = self._decide(base, view)
        content = decision.model_dump_json() if phase.endswith(".extract") else _render(base, decision)
        return AIMessage(content=content)

    def _decide(self, base: str, view: PlayerView) -> BaseModel:
        r = random.Random(f"{self._salt}:{view.seat_no}:{view.quest_no}:{view.attempt}:{base}")
        if base == "propose":
            team = sorted(r.sample(range(1, view.players + 1), view.team_size))
            nos = "、".join(f"{no}号" for no in team)
            return Proposal(team=team, speech=f"（随机）我看 {nos} 就挺合适，出发。", monologue="（随机）闭眼点的将。")
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


def _render(base: str, d: BaseModel) -> str:
    """把假后端的结构化决策渲染回标签行自由文本。"""
    if isinstance(d, Proposal):
        return f"内心戏：{d.monologue}\n发言：{d.speech}\n队伍：{'、'.join(map(str, d.team))}"
    if isinstance(d, TeamVote):
        return f"内心戏：{d.monologue}\n投票：{'赞成' if d.approve else '否决'}"
    if isinstance(d, QuestCard):
        return f"内心戏：{d.monologue}\n出牌：{'成功' if d.success else '破坏'}"
    if isinstance(d, Assassination):
        return f"内心戏：{d.monologue}\n发言：{d.speech}\n刺杀：{d.target_no}"
    if isinstance(d, LadyChoice):
        return f"内心戏：{d.monologue}\n查验：{d.target_no}"
    assert isinstance(d, Statement)
    return f"内心戏：{d.monologue}\n发言：{d.speech}"


class LLMPlayer(LLMActor):
    """Avalon 决策动作；通用 LLM 链路由 LLMActor 提供。"""

    def __init__(self, recorder: Recorder, extractor: ModelSpec, backend: ModelBackend) -> None:
        super().__init__(recorder=recorder, extractor=extractor, backend=backend)

    async def _say(self, phase: str, view: PlayerView) -> str:
        return await self._converse(phase, view, prompts.system_text(view))

    async def _read[T: BaseModel](self, phase: str, view: PlayerView, instruction: str, model_cls: type[T]) -> T:
        return await self._extract(phase, view, prompts.EXTRACT_SYSTEM, instruction, model_cls)

    async def propose(self, view: PlayerView) -> tuple[Proposal, str]:
        async def once() -> tuple[Proposal, str]:
            raw = await self._say("propose", view)
            return await self._read("propose", view, prompts.extract_propose(raw, view.team_size, view.players), Proposal), raw

        return await self._act("propose", view, once)

    async def discuss(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("discuss", view)
            return await self._read("discuss", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("discuss", view, once)

    async def vote_team(self, view: PlayerView) -> tuple[TeamVote, str]:
        async def once() -> tuple[TeamVote, str]:
            raw = await self._say("team_vote", view)
            return await self._read("team_vote", view, prompts.extract_vote(raw), TeamVote), raw

        return await self._act("team_vote", view, once)

    async def play_quest(self, view: PlayerView) -> tuple[QuestCard, str]:
        async def once() -> tuple[QuestCard, str]:
            raw = await self._say("quest", view)
            return await self._read("quest", view, prompts.extract_quest(raw), QuestCard), raw

        return await self._act("quest", view, once)

    async def assassinate(self, view: PlayerView) -> tuple[Assassination, str]:
        async def once() -> tuple[Assassination, str]:
            raw = await self._say("assassinate", view)
            return await self._read("assassinate", view, prompts.extract_assassinate(raw, view.good_suspects()), Assassination), raw

        return await self._act("assassinate", view, once)

    async def lady_choose(self, view: PlayerView) -> tuple[LadyChoice, str]:
        async def once() -> tuple[LadyChoice, str]:
            raw = await self._say("lady_choose", view)
            return await self._read("lady_choose", view, prompts.extract_lady_choice(raw, view.lady_candidates()), LadyChoice), raw

        return await self._act("lady_choose", view, once)

    async def lady_announce(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("lady_announce", view)
            return await self._read("lady_announce", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("lady_announce", view, once)

    async def banter(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._say("banter", view)
            return await self._read("banter", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("banter", view, once)


def build_player(kind: Literal["llm", "random"], rng: random.Random, recorder: Recorder, extractor: ModelSpec) -> Player:
    """边界工厂：llm 注入 LiveBackend，random 注入 Avalon FakeBackend。"""
    backend: ModelBackend = FakeBackend(rng) if kind == "random" else LiveBackend()
    return LLMPlayer(recorder=recorder, extractor=extractor, backend=backend)
