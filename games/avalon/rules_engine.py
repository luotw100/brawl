"""Avalon 的完整 Rules 实现（Sentence Runtime 版）。

语义全在本文件：阶段机、计票、胜负、信息隔离、prompt 构造、JSON 解析校验。
core.sr 只负责驱动循环、调用 actor、记录句子日志与路由。
"""

from __future__ import annotations

import json
import random
from typing import Any

from pydantic import BaseModel, Field

from avalon import prompts, rules
from avalon.state import (
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
from avalon.validation import GameSetup
from avalon.view import PlayerView
from core.parsing import JsonParseError, parse_json
from core.sr.rules import Rules
from core.sr.types import Delivery, Expectation, HostEmit, ParseResult


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
    finished: bool = False
    phase: str = "propose"
    pregame_done: bool = False
    discuss_index: int = 0
    discuss_count: int = 0
    banter_index: int = 0
    reveal_emitted: bool = False
    closing_emitted: bool = False
    lady_target: int | None = None
    lady_seen: str | None = None
    lady_announced: bool = False
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

    def _seat(self, no: int) -> Seat:
        return self.seats[no - 1]

    def _model_spec(self, seat: Seat, registry) -> Any:
        if seat.model is None:
            return None
        return registry.models.get(seat.model)


class AvalonRules(Rules):
    """Avalon 玩法插件：实现 core.sr.rules.Rules 协议。"""

    def __init__(self, registry: Any, settings: Any) -> None:
        self._registry = registry
        self._settings = settings

    def initial_state(self, setup: GameSetup) -> AvalonState:
        seats = []
        for i, role in enumerate(setup.roles):
            no = i + 1
            seats.append(
                Seat(
                    no=no,
                    persona_id=setup.cast[i],
                    strategy_id=setup.strategies[i],
                    model=setup.models[i],
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
        return state.finished or state.phase == "done"

    def turn_id(self, state: AvalonState) -> str:
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
            return f"q{q}.a{a}.lady.choose"
        if state.phase == "lady_announce":
            return f"q{q}.a{a}.lady.announce"
        if state.phase == "assassinate":
            return "assassinate"
        if state.phase == "banter":
            return f"banter.{state.banter_index}"
        return f"{state.phase}"

    def expected(self, state: AvalonState) -> Expectation:
        if state.phase == "propose":
            return Expectation(seats=[state.leader], turn_id=self.turn_id(state))
        if state.phase == "discuss":
            order = state._discuss_order
            return Expectation(seats=[order[state.discuss_index]], turn_id=self.turn_id(state))
        if state.phase == "team_vote":
            return Expectation(seats=list(range(1, state.players + 1)), turn_id=self.turn_id(state))
        if state.phase == "quest":
            return Expectation(seats=list(state.team), turn_id=self.turn_id(state))
        if state.phase == "lady_choose":
            assert state.lady_holder is not None
            return Expectation(seats=[state.lady_holder], turn_id=self.turn_id(state))
        if state.phase == "lady_announce":
            assert state.lady_holder is not None
            return Expectation(seats=[state.lady_holder], turn_id=self.turn_id(state))
        if state.phase == "assassinate":
            assassin = next(s.no for s in state.seats if s.role is Role.ASSASSIN)
            return Expectation(seats=[assassin], turn_id=self.turn_id(state))
        if state.phase == "banter":
            order = state._banter_order
            return Expectation(seats=[order[state.banter_index]], turn_id=self.turn_id(state))
        raise RuntimeError(f"unexpected phase for expected(): {state.phase}")

    def view(self, seat: int, state: AvalonState, inbox: list[str], *, first_speak: bool = False) -> Any:
        s = state._seat(seat)
        q = min(state.quest_no, rules.QUESTS)
        view = PlayerView(
            seat_no=seat,
            role=s.role,
            intel=list(s.intel),
            model=state._model_spec(s, self._registry),
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
            lady_seen=state.lady_seen,
            tone=self._registry.personas[s.persona_id].tone,
            strategy=self._registry.strategies[s.strategy_id].system,
            history=list(state.history),
            messages=[],
            phase=state.phase,
        )
        with_situation = state.phase not in ("assassinate", "lady_announce", "banter")
        mimicry_guard = state.phase != "banter"
        ask = prompts.opencode_ask(state.phase, view)
        context = prompts.situation(view) if with_situation else ""
        view.prompt = prompts.human_message(inbox, context, ask, view.tone, mimicry_guard=mimicry_guard)
        view.prompt = prompts.opencode_prompt(view, include_context=first_speak)
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
        if phase == "assassinate":
            return self._parse_assassinate(seat, raw, state)
        if phase == "banter":
            return self._parse_banter(seat, raw, state)
        raise RuntimeError(f"unexpected phase for parse(): {phase}")

    def _parse_propose(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        q = min(state.quest_no, rules.QUESTS)
        size = rules.team_size(state.players, q)
        result = parse_json(raw, Proposal)
        team = sorted(result.team)
        nos = set(range(1, state.players + 1))
        if len(team) != size or len(set(team)) != size or not set(team) <= nos:
            raise JsonParseError(f"{seat}号 提出非法队伍 {team}（需 {size} 个互不相同的在场座位）")
        deliveries = [
            Delivery(text=prompts.line_player(seat, result.speech), channels=["broadcast"]),
            Delivery(text=prompts.line_team_record(seat, team), channels=["broadcast"]),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "propose", "seat": seat, "team": team,
            "speech": result.speech, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_discuss(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, Statement)
        deliveries = [
            Delivery(text=prompts.line_player(seat, result.speech), channels=["broadcast"]),
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
        deliveries = [
            Delivery(text=prompts.line_player(seat, result.speech), channels=["broadcast"]),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "lady_announce", "seat": seat,
            "speech": result.speech, "monologue": result.monologue,
            "truth": state.lady_seen,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_assassinate(self, seat: int, raw: str, state: AvalonState) -> ParseResult:
        result = parse_json(raw, Assassination)
        s = state._seat(seat)
        # 合法目标 = 除自己与已知同伙外的任何人（候选清单与 view.good_suspects 同源）。
        # 奥伯伦不参与红方互认，刺客认不出他——误刀奥伯伦是合法操作（等于空刀，蓝方胜），
        # 绝不能在校验层拒绝：候选清单给得出、校验却拒收，会把对局卡死在重试上。
        legal = {x.no for x in state.seats if x.no != seat} - set(s.intel)
        if result.target_no not in legal:
            raise JsonParseError(f"刺客 {seat}号 刺向非法目标 {result.target_no}（可选：{sorted(legal)}）")
        target = state._seat(result.target_no)
        hit = target.role is Role.MERLIN
        deliveries = [
            Delivery(text=prompts.line_player(seat, result.speech), channels=["broadcast"]),
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
        deliveries = [
            Delivery(text=prompts.line_player(seat, result.speech), channels=["broadcast"]),
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
            return state.model_copy(deep=True, update={
                "lady_announced": True,
            })
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
        raise RuntimeError(f"unknown action kind: {kind}")

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
            return state.model_copy(deep=True, update={"phase": "lady_announce", "lady_announced": False})
        if phase == "lady_announce":
            return state.model_copy(deep=True, update={
                "phase": "propose",
                "lady_holder": state.lady_target,
                "lady_used": [*state.lady_used, state.lady_target],
                "lady_target": None,
                "lady_seen": None,
                "lady_announced": False,
            })
        if phase == "assassinate":
            return state.model_copy(deep=True, update={"phase": "banter", "banter_index": 0})
        if phase == "banter":
            if state.banter_index + 1 < state.players:
                return state.model_copy(deep=True, update={"banter_index": state.banter_index + 1})
            return state.model_copy(deep=True, update={"phase": "done", "finished": True})
        if phase == "done":
            return state
        raise RuntimeError(f"unexpected phase for advance(): {phase}")

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
            out["phase"] = "assassinate"
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
        if state.phase == "lady_announce" and state.lady_target is not None and not state.lady_announced:
            return self._lady_transfer(state)
        if state.phase == "assassinate" and state.assassination is not None:
            return self._assassination_result(state)
        if state.phase == "banter" and state.banter_index == 0 and not state.reveal_emitted:
            return self._postgame_reveal(state)
        if state.phase == "done" and not state.closing_emitted:
            return self._closing(state)
        return []

    def _pregame(self, state: AvalonState) -> list[HostEmit]:
        seats_payload = [
            {"no": s.no, "persona": s.persona_id, "strategy": s.strategy_id, "role": s.role.value, "side": s.role.side.value, "intel": s.intel, "model": s.model}
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
                turn_id=self.turn_id(state),
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
                turn_id=self.turn_id(state),
                deliveries=[
                    # 票型明细是公开账本（谁投了什么全场可见），与主持结果同句上桌。
                    Delivery(text=prompts.line_votes(state.votes), channels=["broadcast", "audience"]),
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
                turn_id=self.turn_id(state),
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps(action, ensure_ascii=False),
            )
        ]

    def _lady_transfer(self, state: AvalonState) -> list[HostEmit]:
        assert state.lady_holder is not None and state.lady_target is not None
        text = prompts.line_host_lady(state.lady_holder, state.lady_target)
        return [
            HostEmit(
                turn_id=self.turn_id(state),
                deliveries=[Delivery(text=text, channels=["broadcast", "audience"])],
                action=json.dumps({"kind": "lady_transfer", "holder": state.lady_holder, "target": state.lady_target}, ensure_ascii=False),
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
        phase = getattr(view, "phase", "")
        base = phase.removesuffix(".extract") if hasattr(phase, "removesuffix") else phase
        r = random.Random(f"{seat}:{view.seat_no}:{view.quest_no}:{view.attempt}:{base}")

        if base == "propose":
            team = sorted(r.sample(range(1, view.players + 1), view.team_size))
            return Proposal(
                team=team,
                speech=f"（随机）我看 {'、'.join(f'{no}号' for no in team)} 就挺合适，出发。",
                monologue="（随机）闭眼点的将。",
            ).model_dump_json()
        if base == "discuss":
            return Statement(
                speech="（随机）这队我说不上好坏，先看看大家的票。",
                monologue="（随机）水一句，别看我。",
            ).model_dump_json()
        if base == "team_vote":
            return TeamVote(approve=r.random() < 0.5, monologue="（随机）抛硬币定的。").model_dump_json()
        if base == "quest":
            success = True if view.role.side is Side.GOOD else r.random() < 0.5
            return QuestCard(success=success, monologue="（随机）牌已扣下，面无表情。").model_dump_json()
        if base == "assassinate":
            target = r.choice(view.good_suspects())
            return Assassination(
                target_no=target,
                speech=f"（随机）直觉告诉我，{target}号就是梅林。",
                monologue="（随机）蒙的，但要蒙得自信。",
            ).model_dump_json()
        if base == "lady_choose":
            return LadyChoice(target_no=r.choice(view.lady_candidates()), monologue="（随机）就查你了。").model_dump_json()
        if base == "lady_announce":
            claim = view.lady_seen if r.random() < 0.5 else ("evil" if view.lady_seen == "good" else "good")
            side = "蓝方" if claim == "good" else "红方"
            return Statement(
                speech=f"（随机）我查验了{view.lady_target}号——{side}。",
                monologue="（随机）信不信由你们。",
            ).model_dump_json()
        if base == "banter":
            return Statement(
                speech="（随机）这局打完了，各位辛苦，下把再战。",
                monologue="（随机）复盘就免了，赢牌全靠脸。",
            ).model_dump_json()
        raise ValueError(f"未知决策阶段：{base}")

    def pack(self, state: AvalonState) -> bytes:
        return state.model_dump_json().encode("utf-8")

    def unpack(self, raw: bytes) -> AvalonState:
        return AvalonState.model_validate_json(raw)
