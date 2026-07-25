"""Avalon 的完整 Rules 实现（Sentence Runtime 版）。

语义全在本文件：阶段机、计票、胜负、信息隔离、prompt 构造、JSON 解析校验。
brawl.runtime 只负责驱动循环、调用 actor、记录句子日志与路由。
"""

from __future__ import annotations

import json
import random
from typing import Any

from pydantic import BaseModel, Field

from brawl.games.avalon import prompts, rules
from brawl.games.avalon.state import (
    Assassination,
    LadyChoice,
    Proposal,
    ProposalRecord,
    QuestCard,
    Role,
    Seat,
    Side,
    Statement,
    TeamVote,
)
from brawl.games.avalon.validation import GameSetup
from brawl.games.avalon.view import PlayerView
from brawl.runtime.parsing import JsonParseError, action_schema, parse_json
from brawl.runtime.rules import Rules
from brawl.runtime.threads import SeatThread
from brawl.runtime.types import Delivery, Expectation, HostEmit, ParseResult


class LadyTransfer(BaseModel):
    """已经生效、等待主持人同事务播报的仙女令牌转移。"""

    holder: int
    target: int


# 每个行动阶段的 StructuredOutput schema，模块加载时一次性冻结。
_PHASE_SCHEMAS: dict[str, dict[str, Any]] = {
    phase: action_schema(model, phase)
    for phase, model in {
        "propose": Proposal,
        "discuss": Statement,
        "team_vote": TeamVote,
        "quest": QuestCard,
        "lady_choose": LadyChoice,
        "lady_announce": Statement,
        "evil_chat": Statement,
        "assassinate": Assassination,
        "banter": Statement,
    }.items()
}


class AvalonState(BaseModel):
    """对局状态：对 core 不透明，仅由 AvalonRules 管理。"""

    seats: list[Seat]
    leader: int
    rejections: int = 0
    team: list[int] = Field(default_factory=list)
    votes: dict[int, bool] = Field(default_factory=dict)
    cards: dict[int, bool] = Field(default_factory=dict)
    history: list[ProposalRecord] = Field(default_factory=list)
    lady_holder: int | None = None
    lady_used: list[int] = Field(default_factory=list)
    winner: str | None = None
    win_reason: str | None = None
    phase: str = "propose"
    pregame_done: bool = False
    discuss_index: int = 0
    discuss_count: int = 0
    evil_chat_index: int = 0
    evil_chat_opened: bool = False
    evil_team: list[int] = Field(default_factory=list)
    banter_index: int = 0
    reveal_emitted: bool = False
    lady_target: int | None = None
    lady_seen: str | None = None
    lady_transfer: LadyTransfer | None = None
    assassination: dict[str, Any] | None = None

    @property
    def players(self) -> int:
        return len(self.seats)

    @property
    def quest_no(self) -> int:
        return sum(1 for r in self.history if r.result is not None) + 1

    @property
    def attempt(self) -> int:
        return self.rejections + 1

    @property
    def quests_succeeded(self) -> int:
        return sum(1 for r in self.history if r.result == "success")

    @property
    def quests_failed(self) -> int:
        return sum(1 for r in self.history if r.result == "fail")

    @property
    def next_leader(self) -> int:
        return self.leader % self.players + 1

    @property
    def _discuss_order(self) -> list[int]:
        """从队长下家起，除队长外全员按座位序发言。"""
        n = self.players
        return [((self.leader + i - 1) % n) + 1 for i in range(1, n)]

    @property
    def _banter_order(self) -> list[int]:
        """败方先复盘，组内按座位号。"""
        return sorted((s.no for s in self.seats), key=lambda no: (self.seats[no - 1].role.side.value == self.winner, no))

    @property
    def _evil_chat_order(self) -> list[int]:
        return list(self.evil_team)

    def _seat(self, no: int) -> Seat:
        return self.seats[no - 1]

class AvalonRules(Rules):
    """Avalon 玩法插件：实现 brawl.runtime.rules.Rules 协议。"""

    def initial_state(self, setup: GameSetup) -> AvalonState:
        seats = []
        for i, role in enumerate(setup.roles):
            no = i + 1
            seats.append(
                Seat(
                    no=no,
                    persona_id=setup.cast[i],
                    strategy_id=setup.strategies[i],
                    tone=setup.tones[i],
                    strategy=setup.strategy_systems[i],
                    role=role,
                    intel=rules.night_intel(setup.roles, no),
                )
            )
        return AvalonState(
            seats=seats,
            leader=setup.first_leader,
            lady_holder=setup.lady_holder,
            lady_used=[setup.lady_holder] if setup.lady_holder is not None else [],
        )

    def is_terminal(self, state: AvalonState) -> bool:
        return state.phase == "done"

    def _turn_id(self, state: AvalonState) -> str:
        q = min(state.quest_no, rules.QUESTS)
        a = state.attempt
        if state.phase == "propose":
            return f"q{q}.a{a}.propose"
        if state.phase == "discuss":
            return f"q{q}.a{a}.discuss.{state.discuss_index}"
        if state.phase == "team_vote":
            return f"q{q}.a{a}.team_vote"
        if state.phase == "quest":
            return f"q{q}.a{a}.quest"
        if state.phase == "lady_choose":
            return f"q{max(1, state.quest_no - 1)}.lady.choose"
        if state.phase == "lady_announce":
            return f"q{max(1, state.quest_no - 1)}.lady.announce"
        if state.phase == "evil_chat":
            return f"evil_chat.{state.evil_chat_index}"
        if state.phase == "assassinate":
            return "assassinate"
        if state.phase == "banter":
            return f"banter.{state.banter_index}"
        return f"{state.phase}"

    def expected(self, state: AvalonState) -> Expectation:
        if state.phase == "propose":
            return Expectation(seats=[state.leader], turn_id=self._turn_id(state))
        if state.phase == "discuss":
            order = state._discuss_order
            return Expectation(seats=[order[state.discuss_index]], turn_id=self._turn_id(state))
        if state.phase == "team_vote":
            return Expectation(seats=list(range(1, state.players + 1)), turn_id=self._turn_id(state))
        if state.phase == "quest":
            return Expectation(seats=list(state.team), turn_id=self._turn_id(state))
        if state.phase == "lady_choose":
            assert state.lady_holder is not None
            return Expectation(seats=[state.lady_holder], turn_id=self._turn_id(state))
        if state.phase == "lady_announce":
            assert state.lady_holder is not None
            return Expectation(seats=[state.lady_holder], turn_id=self._turn_id(state))
        if state.phase == "evil_chat":
            return Expectation(
                seats=[state._evil_chat_order[state.evil_chat_index]],
                turn_id=self._turn_id(state),
            )
        if state.phase == "assassinate":
            assassin = next(s.no for s in state.seats if s.role is Role.ASSASSIN)
            return Expectation(seats=[assassin], turn_id=self._turn_id(state))
        if state.phase == "banter":
            order = state._banter_order
            return Expectation(seats=[order[state.banter_index]], turn_id=self._turn_id(state))
        raise RuntimeError(f"expected() 遇到意外阶段：{state.phase}")

    def view(self, seat: int, state: AvalonState, thread: SeatThread, *, first_speak: bool = False) -> Any:
        expected = self.expected(state)
        if seat not in expected.seats:
            raise ValueError(
                f"seat {seat} 无权读取 turn {expected.turn_id} 的行动视图；"
                f"当前行动席位是 {expected.seats}"
            )
        s = state._seat(seat)
        q = min(state.quest_no, rules.QUESTS)
        view = PlayerView(
            seat_no=seat,
            role=s.role,
            intel=list(s.intel),
            lineup=sorted((s.role for s in state.seats), key=tuple(Role).index),
            players=state.players,
            leader_no=state.leader,
            quest_no=state.quest_no,
            attempt=state.attempt,
            team_size=rules.team_size(state.players, q),
            fails_required=rules.fails_required(state.players, q),
            succeeded=state.quests_succeeded,
            failed=state.quests_failed,
            team=list(state.team),
            lady_used=list(state.lady_used),
            lady_target=state.lady_target,
            lady_seen=state.lady_seen if seat == state.lady_holder else None,
            tone=s.tone,
            strategy=s.strategy,
            history=list(state.history),
            messages=list(thread.messages),
            phase=state.phase,
            evil_team=list(state.evil_team),
        )
        with_situation = state.phase not in ("evil_chat", "assassinate", "lady_announce", "banter")
        mimicry_guard = state.phase not in ("evil_chat", "banter")
        ask = prompts.opencode_ask(state.phase, view)
        context = prompts.situation(view) if with_situation else ""
        view.turn_prompt = prompts.human_message(
            thread.inbox, context, ask, view.tone, mimicry_guard=mimicry_guard
        )
        view.prompt = prompts.opencode_prompt(view, include_context=first_speak)
        view.schema = _PHASE_SCHEMAS[state.phase]
        return view

    def parse(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        phase = state.phase
        if phase == "propose":
            return self._parse_propose(seat, raw, state)
        if phase == "discuss":
            return self._parse_discuss(seat, raw, state)
        if phase == "team_vote":
            return self._parse_team_vote(seat, raw, state)
        if phase == "quest":
            return self._parse_quest(seat, raw, state)
        if phase == "lady_choose":
            return self._parse_lady_choose(seat, raw, state)
        if phase == "lady_announce":
            return self._parse_lady_announce(seat, raw, state)
        if phase == "evil_chat":
            return self._parse_evil_chat(seat, raw, state)
        if phase == "assassinate":
            return self._parse_assassinate(seat, raw, state)
        if phase == "banter":
            return self._parse_banter(seat, raw, state)
        raise RuntimeError(f"parse() 遇到意外阶段：{phase}")

    def _parse_propose(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        q = min(state.quest_no, rules.QUESTS)
        size = rules.team_size(state.players, q)
        result = parse_json(raw, Proposal)
        team = sorted(result.team)
        nos = set(range(1, state.players + 1))
        if len(team) != size or len(set(team)) != size or not set(team) <= nos:
            raise JsonParseError(f"{seat}号 提出非法队伍 {team}（需 {size} 个互不相同的在场座位）")
        deliveries = [
            Delivery(text=result.speech, channels=["broadcast"], kind="speech"),
            Delivery(text=prompts.line_team_record(seat, team), channels=["broadcast"], spoken=False),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "propose", "seat": seat, "team": team,
            "speech": result.speech, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_discuss(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, Statement)
        if result.phase != "discuss":
            raise JsonParseError(f"动作 phase 应为 discuss，实际为 {result.phase}")
        deliveries = [
            Delivery(text=result.speech, channels=["broadcast"], kind="speech"),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "discuss", "seat": seat,
            "speech": result.speech, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_team_vote(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, TeamVote)
        deliveries = [Delivery(text=result.monologue, channels=["audience"])]
        action = json.dumps({
            "kind": "team_vote", "seat": seat,
            "approve": result.approve, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_quest(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        s = state._seat(seat)
        result = parse_json(raw, QuestCard)
        if s.role.side is Side.GOOD and not result.success:
            raise JsonParseError(f"{seat}号 是蓝方却出了破坏票——蓝方只能出成功")
        deliveries = [Delivery(text=result.monologue, channels=["audience"])]
        action = json.dumps({
            "kind": "quest", "seat": seat,
            "success": result.success, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_lady_choose(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, LadyChoice)
        candidates = set(range(1, state.players + 1)) - set(state.lady_used)
        if result.target_no not in candidates:
            raise JsonParseError(f"{seat}号 查验了非法对象 {result.target_no}")
        deliveries = [Delivery(text=result.monologue, channels=["audience"])]
        action = json.dumps({
            "kind": "lady_choose", "seat": seat,
            "target_no": result.target_no, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_lady_announce(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, Statement)
        if result.phase != "lady_announce":
            raise JsonParseError(f"动作 phase 应为 lady_announce，实际为 {result.phase}")
        deliveries = [
            Delivery(text=result.speech, channels=["broadcast"], kind="speech"),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "lady_announce", "seat": seat,
            "speech": result.speech, "monologue": result.monologue,
            "truth": state.lady_seen,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_evil_chat(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, Statement)
        if result.phase != "evil_chat":
            raise JsonParseError(f"动作 phase 应为 evil_chat，实际为 {result.phase}")
        evil_channels: list[str | int] = [*state._evil_chat_order, "audience"]
        deliveries = [
            Delivery(text=result.speech, channels=evil_channels, kind="speech"),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps(
            {
                "kind": "evil_chat",
                "seat": seat,
                "speech": result.speech,
                "monologue": result.monologue,
            },
            ensure_ascii=False,
        )
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_assassinate(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, Assassination)
        # 终局互认已把完整红方名单明确交给刺客；合法目标由这份已披露名单自然投影，
        # 不在这里借裁判 role 真值另做一套隐形过滤。
        legal = {x.no for x in state.seats} - set(state.evil_team)
        if result.target_no not in legal:
            raise JsonParseError(f"刺客 {seat}号 刺向非法目标 {result.target_no}（可选：{sorted(legal)}）")
        target = state._seat(result.target_no)
        hit = target.role is Role.MERLIN
        deliveries = [
            Delivery(text=result.speech, channels=["broadcast"], kind="speech"),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "assassinate", "seat": seat,
            "target_no": result.target_no, "speech": result.speech,
            "monologue": result.monologue, "hit": hit,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_banter(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, Statement)
        if result.phase != "banter":
            raise JsonParseError(f"动作 phase 应为 banter，实际为 {result.phase}")
        deliveries = [
            Delivery(text=result.speech, channels=["broadcast"], kind="speech"),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "banter", "seat": seat,
            "speech": result.speech, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def apply(self, state: AvalonState, action: str) -> AvalonState:
        a = json.loads(action)
        kind = a["kind"]
        if kind == "propose":
            return state.model_copy(deep=True, update={
                "team": list(a["team"]),
                "pregame_done": True,
                "lady_transfer": None,
            })
        if kind == "discuss":
            return state.model_copy(deep=True, update={
                "discuss_count": state.discuss_count + 1,
            })
        if kind == "team_vote":
            return state.model_copy(deep=True, update={
                "votes": {**state.votes, a["seat"]: a["approve"]},
            })
        if kind == "quest":
            return state.model_copy(deep=True, update={
                "cards": {**state.cards, a["seat"]: a["success"]},
            })
        if kind == "lady_choose":
            target = state._seat(a["target_no"])
            return state.model_copy(deep=True, update={
                "lady_target": a["target_no"],
                "lady_seen": target.role.side.value,
            })
        if kind == "lady_announce":
            return state
        if kind == "evil_chat":
            return state.model_copy(deep=True, update={"evil_chat_opened": True})
        if kind == "assassinate":
            return state.model_copy(deep=True, update={
                "assassination": dict(a),
                "winner": "evil" if a["hit"] else "good",
                "win_reason": "assassination",
            })
        if kind == "banter":
            return state.model_copy(deep=True, update={
                "reveal_emitted": True,
            })
        raise RuntimeError(f"未知 action kind：{kind}")

    def advance(self, state: AvalonState) -> AvalonState:
        phase = state.phase
        if phase == "propose":
            return state.model_copy(deep=True, update={"phase": "discuss", "discuss_index": 0, "discuss_count": 0})
        if phase == "discuss":
            if state.discuss_index + 1 < state.players - 1:
                return state.model_copy(deep=True, update={"discuss_index": state.discuss_index + 1})
            return state.model_copy(deep=True, update={"phase": "team_vote"})
        if phase == "team_vote":
            return self._advance_after_vote(state)
        if phase == "quest":
            return self._advance_after_quest(state)
        if phase == "lady_choose":
            return state.model_copy(deep=True, update={"phase": "lady_announce"})
        if phase == "lady_announce":
            assert state.lady_holder is not None and state.lady_target is not None
            return state.model_copy(deep=True, update={
                "phase": "propose",
                "lady_holder": state.lady_target,
                "lady_used": [*state.lady_used, state.lady_target],
                "lady_transfer": LadyTransfer(
                    holder=state.lady_holder,
                    target=state.lady_target,
                ),
                "lady_target": None,
                "lady_seen": None,
            })
        if phase == "evil_chat":
            if state.evil_chat_index + 1 < len(state._evil_chat_order):
                return state.model_copy(
                    deep=True,
                    update={"evil_chat_index": state.evil_chat_index + 1},
                )
            return state.model_copy(deep=True, update={"phase": "assassinate"})
        if phase == "assassinate":
            return state.model_copy(deep=True, update={"phase": "banter", "banter_index": 0})
        if phase == "banter":
            if state.banter_index + 1 < state.players:
                return state.model_copy(deep=True, update={"banter_index": state.banter_index + 1})
            return state.model_copy(deep=True, update={"phase": "done"})
        if phase == "done":
            return state
        raise RuntimeError(f"advance() 遇到意外阶段：{phase}")

    def _advance_after_vote(self, state: AvalonState) -> AvalonState:
        approved = sum(state.votes.values()) * 2 > state.players
        if approved:
            return state.model_copy(deep=True, update={"phase": "quest", "cards": {}})
        rejections = state.rejections + 1
        # 被否提案也进公开账本（旧引擎行为）：谁在几号第几次提队提了谁、票型如何，全场可见。
        record = ProposalRecord(
            quest=state.quest_no,
            attempt=state.attempt,
            leader_no=state.leader,
            team=list(state.team),
            votes=dict(state.votes),
            approved=False,
        )
        out: dict[str, Any] = {
            "phase": "propose",
            "rejections": rejections,
            "leader": state.next_leader,
            "team": [],
            "votes": {},
            "history": [*state.history, record],
        }
        if rejections >= rules.MAX_REJECTIONS:
            out["winner"] = "evil"
            out["win_reason"] = "rejections"
            out["phase"] = "banter"
            out["banter_index"] = 0
        return state.model_copy(deep=True, update=out)

    def _advance_after_quest(self, state: AvalonState) -> AvalonState:
        need = rules.fails_required(state.players, state.quest_no)
        fails = sum(1 for success in state.cards.values() if not success)
        result = "fail" if fails >= need else "success"
        record = ProposalRecord(
            quest=state.quest_no,
            attempt=state.attempt,
            leader_no=state.leader,
            team=list(state.team),
            votes=dict(state.votes),
            approved=True,
            fails=fails,
            result=result,
        )
        succeeded = state.quests_succeeded + (1 if result == "success" else 0)
        failed = state.quests_failed + (1 if result == "fail" else 0)
        next_leader = state.next_leader
        out: dict[str, Any] = {
            "history": [*state.history, record],
            "rejections": 0,
            "leader": next_leader,
            "team": [],
            "votes": {},
            "cards": {},
        }
        if failed >= rules.QUESTS_TO_WIN:
            out["winner"] = "evil"
            out["win_reason"] = "quests"
            out["phase"] = "banter"
            out["banter_index"] = 0
        elif succeeded >= rules.QUESTS_TO_WIN:
            evil_nos = [s.no for s in state.seats if s.role.side is Side.EVIL]
            out["phase"] = "evil_chat"
            out["evil_chat_index"] = 0
            out["evil_chat_opened"] = False
            out["evil_team"] = evil_nos
        elif state.lady_holder is not None and state.quest_no in rules.LADY_QUESTS:
            out["phase"] = "lady_choose"
        else:
            out["phase"] = "propose"
        return state.model_copy(deep=True, update=out)

    def pending(self, state: AvalonState) -> list[HostEmit]:
        if not state.pregame_done and state.phase == "propose":
            return self._pregame(state)
        if state.phase == "discuss" and state.discuss_count == 0 and state.team:
            return self._team_intro(state)
        if state.phase == "team_vote" and len(state.votes) == state.players:
            return self._vote_result(state)
        if state.phase == "quest" and len(state.cards) == len(state.team):
            return self._quest_result(state)
        if state.phase == "lady_choose" and state.lady_target is not None:
            return self._lady_choice(state)
        if state.lady_transfer is not None:
            return self._lady_transfer(state)
        if state.phase == "evil_chat" and not state.evil_chat_opened:
            return self._evil_chat_opening(state)
        if state.phase == "assassinate" and state.assassination is not None:
            return self._assassination_result(state)
        if state.phase == "banter" and state.banter_index == 0 and not state.reveal_emitted:
            return self._postgame_reveal(state)
        if state.phase == "done":
            return self._closing(state)
        return []

    def _pregame(self, state: AvalonState) -> list[HostEmit]:
        seats_payload = [
            {"no": s.no, "persona": s.persona_id, "strategy": s.strategy_id, "role": s.role.value, "side": s.role.side.value, "intel": s.intel}
            for s in state.seats
        ]
        first_team_size = rules.team_size(state.players, 1)
        action = {
            "kind": "game_start",
            "players": state.players,
            "good": sum(1 for s in state.seats if s.role.side is Side.GOOD),
            "evil": sum(1 for s in state.seats if s.role.side is Side.EVIL),
            "seats": seats_payload,
        }
        text = prompts.line_host_opening(
            state.players, state.lady_holder, state.leader, first_team_size
        )
        return [
            HostEmit(
                turn_id="pregame.opening",
                deliveries=[
                    Delivery(text=text, channels=["broadcast", "audience"]),
                ],
                action=json.dumps(action, ensure_ascii=False),
            )
        ]

    def _team_intro(self, state: AvalonState) -> list[HostEmit]:
        first = state._discuss_order[0]
        text = prompts.line_host_team(first)
        return [
            HostEmit(
                turn_id=self._turn_id(state),
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps({"kind": "team_intro", "team": state.team}, ensure_ascii=False),
            )
        ]

    def _vote_result(self, state: AvalonState) -> list[HostEmit]:
        approves = sum(state.votes.values())
        total = state.players
        approved = approves * 2 > total
        next_leader = state.leader % state.players + 1
        result_text = prompts.line_host_vote_result(
            approves,
            total,
            approved,
            state.rejections if approved else state.rejections + 1,
            None if approved or (state.rejections + 1) >= rules.MAX_REJECTIONS else next_leader,
        )
        action = {
            "kind": "vote_result",
            "votes": dict(state.votes),
            "approved": approved,
            "rejections": state.rejections if approved else state.rejections + 1,
        }
        return [
            HostEmit(
                turn_id=self._turn_id(state),
                deliveries=[
                    # 票型是全员可见的文字账本，但符号串不适合配音；口语结果由主持人念出。
                    Delivery(
                        text=prompts.line_votes(state.votes),
                        channels=["broadcast", "audience"],
                        spoken=False,
                    ),
                    Delivery(text=result_text, channels=["broadcast", "audience"]),
                ],
                action=json.dumps(action, ensure_ascii=False),
            )
        ]

    def _quest_result(self, state: AvalonState) -> list[HostEmit]:
        need = rules.fails_required(state.players, state.quest_no)
        fails = sum(1 for success in state.cards.values() if not success)
        result = "fail" if fails >= need else "success"
        succeeded = state.quests_succeeded + (1 if result == "success" else 0)
        failed = state.quests_failed + (1 if result == "fail" else 0)
        terminal = failed >= rules.QUESTS_TO_WIN or succeeded >= rules.QUESTS_TO_WIN
        lady_next = state.lady_holder is not None and state.quest_no in rules.LADY_QUESTS
        next_leader = state.leader % state.players + 1
        text = prompts.line_host_quest_result(
            state.quest_no,
            fails,
            result,
            succeeded,
            failed,
            None if terminal or lady_next else next_leader,
        )
        action = {
            "kind": "quest_result",
            "quest": state.quest_no,
            "fails": fails,
            "fails_required": need,
            "result": result,
        }
        return [
            HostEmit(
                turn_id=self._turn_id(state),
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps(action, ensure_ascii=False),
            )
        ]

    def _lady_transfer(self, state: AvalonState) -> list[HostEmit]:
        assert state.lady_transfer is not None
        transfer = state.lady_transfer
        text = prompts.line_host_lady_transfer(transfer.holder, transfer.target)
        return [
            HostEmit(
                turn_id=(
                    f"q{max(1, state.quest_no - 1)}.lady.transfer."
                    f"{transfer.holder}.{transfer.target}"
                ),
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps(
                    {
                        "kind": "lady_transfer",
                        "holder": transfer.holder,
                        "target": transfer.target,
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    def _lady_choice(self, state: AvalonState) -> list[HostEmit]:
        assert state.lady_holder is not None and state.lady_target is not None
        text = prompts.line_host_lady_choice(state.lady_holder, state.lady_target)
        return [
            HostEmit(
                turn_id=(
                    f"q{max(1, state.quest_no - 1)}.lady.choice."
                    f"{state.lady_holder}.{state.lady_target}"
                ),
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps(
                    {
                        "kind": "lady_choice",
                        "holder": state.lady_holder,
                        "target": state.lady_target,
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    def _evil_chat_opening(self, state: AvalonState) -> list[HostEmit]:
        roles = {
            seat.no: seat.role
            for seat in state.seats
            if seat.role.side is Side.EVIL
        }
        channels: list[str | int] = [*roles, "audience"]
        return [
            HostEmit(
                turn_id="evil_chat.opening",
                deliveries=[
                    Delivery(
                        text=prompts.line_host_evil_chat(roles),
                        channels=channels,
                    )
                ],
                action=json.dumps(
                    {
                        "kind": "evil_chat_opening",
                        "roles": {no: role.value for no, role in roles.items()},
                    },
                    ensure_ascii=False,
                ),
            )
        ]

    def _assassination_result(self, state: AvalonState) -> list[HostEmit]:
        assert state.assassination is not None
        assassin_no = state.assassination["seat"]
        target_no = state.assassination["target_no"]
        hit = state.assassination["hit"]
        text = prompts.line_host_assassination(assassin_no, target_no, hit)
        return [
            HostEmit(
                turn_id="postgame.assassination",
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps({"kind": "assassination_result", "assassin": assassin_no, "target": target_no, "hit": hit}, ensure_ascii=False),
            )
        ]

    def _postgame_reveal(self, state: AvalonState) -> list[HostEmit]:
        roles = {s.no: s.role for s in state.seats}
        text = prompts.line_host_reveal(state.winner, state.win_reason, roles)
        return [
            HostEmit(
                turn_id="postgame.reveal",
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps({"kind": "reveal", "winner": state.winner, "win_reason": state.win_reason, "roles": {no: r.value for no, r in roles.items()}}, ensure_ascii=False),
            )
        ]

    def _closing(self, state: AvalonState) -> list[HostEmit]:
        text = prompts.line_host_closing()
        return [
            HostEmit(
                turn_id="postgame.closing",
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps({"kind": "closing"}, ensure_ascii=False),
            )
        ]

    def random_action(self, seat: int, view: PlayerView) -> str:
        """零网络 sampler：为当前 phase 生成合法 JSON raw。"""
        phase = view.phase
        r = random.Random(f"{seat}:{view.seat_no}:{view.quest_no}:{view.attempt}:{phase}")

        if phase == "propose":
            team = sorted(r.sample(range(1, view.players + 1), view.team_size))
            return Proposal(
                phase="propose",
                team=team,
                speech=f"（随机）我看 {'、'.join(f'{no}号' for no in team)} 就挺合适，出发。",
                monologue="（随机）闭眼点的将。",
            ).model_dump_json()
        if phase == "discuss":
            return Statement(
                phase="discuss",
                speech="（随机）这队我说不上好坏，先看看大家的票。",
                monologue="（随机）水一句，别看我。",
            ).model_dump_json()
        if phase == "team_vote":
            return TeamVote(
                phase="team_vote",
                approve=r.random() < 0.5,
                monologue="（随机）抛硬币定的。",
            ).model_dump_json()
        if phase == "quest":
            success = True if view.role.side is Side.GOOD else r.random() < 0.5
            return QuestCard(
                phase="quest",
                success=success,
                monologue="（随机）牌已扣下，面无表情。",
            ).model_dump_json()
        if phase == "assassinate":
            target = r.choice(view.good_suspects())
            return Assassination(
                phase="assassinate",
                target_no=target,
                speech=f"（随机）直觉告诉我，{target}号就是梅林。",
                monologue="（随机）蒙的，但要蒙得自信。",
            ).model_dump_json()
        if phase == "evil_chat":
            return Statement(
                phase="evil_chat",
                speech="（随机）红方身份已经互认，我的梅林线索都在公开轨迹里，刺客按长程表现判断。",
                monologue="（随机）终于不用再装蓝方了。",
            ).model_dump_json()
        if phase == "lady_choose":
            return LadyChoice(
                phase="lady_choose",
                target_no=r.choice(view.lady_candidates()),
                monologue="（随机）就查你了。",
            ).model_dump_json()
        if phase == "lady_announce":
            claim = view.lady_seen if r.random() < 0.5 else ("evil" if view.lady_seen == "good" else "good")
            side = "蓝方" if claim == "good" else "红方"
            return Statement(
                phase="lady_announce",
                speech=f"（随机）我查验了{view.lady_target}号——{side}。",
                monologue="（随机）信不信由你们。",
            ).model_dump_json()
        if phase == "banter":
            return Statement(
                phase="banter",
                speech="（随机）这局打完了，各位辛苦，下把再战。",
                monologue="（随机）复盘就免了，赢牌全靠脸。",
            ).model_dump_json()
        raise ValueError(f"未知决策阶段：{phase}")
