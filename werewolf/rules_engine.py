"""Werewolf 的完整 Rules 实现（Sentence Runtime 版）。

语义全在本文件：昼夜阶段机、狼刀多数决、药瓶 bookkeeping、白痴翻牌、猎人开枪、
屠边判负、信息隔离、prompt 构造、JSON 解析校验。core.runtime 只负责驱动循环、调用 actor、
记录句子日志与路由。

host 串场词机制（host_outbox）：apply/advance 把要播报的 HostEmit 放进 state.host_outbox，
pending() 原样读出；loop 在每次 apply/advance 后 flush 一次并落日志。**每条 apply/advance
都必须显式重写 host_outbox**（无播报就置 []）——outbox 的生命周期只到紧随其后的那次
flush，绝不跨轮携带。这条不变量保证：同一串场词不会重发，resume 重放时逐字重建。

昼夜时序（贴合真人桌）：夜里狼刀→预言家→女巫各自暗行动，advance 只结算不播报；天亮
dawn 播报死讯与翻牌，死者留遗言，随后死亡的猎人才开枪，最后判胜负。枪在结算之前、
天亮之后——夜死的猎人不会在黑夜里抢先开枪。
"""

from __future__ import annotations

import json
import random
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from core.runtime.parsing import JsonParseError, parse_json
from core.runtime.threads import SeatThread
from core.runtime.types import Delivery, Expectation, HostEmit, ParseResult
from werewolf import prompts, rules
from werewolf.state import (
    DeathRecord,
    HunterShot,
    Seat,
    SeerAction,
    SeerCheck,
    Statement,
    VoteAction,
    VoteRecord,
    WitchAction,
    WolfKill,
)
from werewolf.validation import GameSetup
from werewolf.view import PlayerView, PublicDeath


def _emit(turn_id: str, text: str, channels: list, action: dict) -> HostEmit:
    return HostEmit(
        turn_id=turn_id,
        deliveries=[Delivery(text=text, channels=channels)],
        action=json.dumps(action, ensure_ascii=False),
    )


def _audience_event(turn_id: str, text: str, action: dict[str, Any]) -> HostEmit:
    """只写上帝视角账本的确定性裁判事件，不进入任何玩家 inbox，也不交给 TTS。"""
    return HostEmit(
        turn_id=turn_id,
        deliveries=[Delivery(text=text, channels=["audience"], spoken=False)],
        action=json.dumps(action, ensure_ascii=False),
    )


class WerewolfState(BaseModel):
    """对局状态：对 core 不透明，仅由 WerewolfRules 管理。"""

    seats: list[Seat]
    board: str
    day: int = 1
    phase: str = "night_wolf"
    deaths: list[DeathRecord] = Field(default_factory=list)
    round_deaths: list[DeathRecord] = Field(default_factory=list)
    votes_history: list[VoteRecord] = Field(default_factory=list)
    seer_checks: list[SeerCheck] = Field(default_factory=list)
    # 当夜狼队提议（seat/target/speech 由 action 落日志，这里只留计票所需）
    wolf_proposals: list[dict[str, Any]] = Field(default_factory=list)
    wolf_index: int = 0
    wolf_target: int | None = None
    witch_saved: bool = False
    witch_poison_target: int | None = None
    witch_has_antidote: bool = True
    witch_has_poison: bool = True
    pending_hunter: int | None = None  # 待开枪的猎人座位（死于刀/放逐才有，死于毒为 None）
    hunter_action: dict[str, Any] | None = None  # 已解析、待 advance 结算的猎人开枪
    lw_queue: list[int] = Field(default_factory=list)  # 本轮遗言发言人（座位序，不含待开枪猎人）
    lw_index: int = 0
    discuss_index: int = 0
    vote_round: dict[int, int] = Field(default_factory=dict)  # 当天放逐票：voter → target
    origin: str = "night"  # 本论死亡链的来源：night=夜死，vote=放逐
    banter_index: int = 0
    winner: str | None = None
    win_reason: str | None = None
    host_outbox: list[HostEmit] = Field(default_factory=list)

    @property
    def players(self) -> int:
        return len(self.seats)

    def living(self) -> list[Seat]:
        return [s for s in self.seats if s.alive]

    def living_nos(self) -> list[int]:
        return [s.no for s in self.living()]

    def seat(self, no: int) -> Seat:
        return self.seats[no - 1]

    def living_wolves(self) -> list[Seat]:
        return sorted((s for s in self.living() if s.role is rules.Role.WEREWOLF), key=lambda s: s.no)

    @property
    def _banter_order(self) -> list[int]:
        """败方先复盘，组内按座位号。"""
        return [s.no for s in sorted(self.seats, key=lambda s: (s.side.value == self.winner, s.no))]


class WerewolfRules:
    """Werewolf 玩法插件：实现 core.runtime.rules.Rules 协议。"""

    # ---------------------------------------------------------------- 循环驱动

    def initial_state(self, setup: GameSetup) -> WerewolfState:
        # 角色已在边界摇定（回放确定性不变量：对局过程零裁判侧随机）。
        seats = [
            Seat(
                no=i + 1,
                persona_id=setup.cast[i],
                strategy_id=setup.strategies[i],
                tone=setup.tones[i],
                strategy_system=setup.strategy_systems[i],
                model=setup.models[i],
                role=role,
            )
            for i, role in enumerate(setup.roles)
        ]
        board = rules.board_by_id(setup.board)
        opening = _emit(
            "pregame.opening",
            prompts.line_host_opening(board.name, board.players, board.wolves),
            ["broadcast", "audience"],
            {
                "kind": "game_start",
                "board": board.id,
                "board_name": board.name,
                "players": board.players,
                "wolves": board.wolves,
                "gods": board.gods,
                "villagers": board.villagers,
                "seats": self._seats_payload(seats),
            },
        )
        night = _emit(
            "day1.night_start",
            prompts.line_host_night(1),
            ["broadcast", "audience"],
            {"kind": "night_start", "day": 1},
        )
        return WerewolfState(seats=seats, board=board.id, host_outbox=[opening, night])

    def is_terminal(self, state: WerewolfState) -> bool:
        return state.phase == "done"

    def turn_id(self, state: WerewolfState) -> str:
        d = state.day
        if state.phase == "night_wolf":
            return f"day{d}.wolf.{state.wolf_index}"
        if state.phase == "night_seer":
            return f"day{d}.seer"
        if state.phase == "night_witch":
            return f"day{d}.witch"
        if state.phase == "last_words":
            return f"day{d}.last_words.{state.lw_index}"
        if state.phase == "hunter_shot":
            return f"day{d}.hunter"
        if state.phase == "day_discuss":
            return f"day{d}.discuss.{state.discuss_index}"
        if state.phase == "day_vote":
            return f"day{d}.vote"
        if state.phase == "banter":
            return f"banter.{state.banter_index}"
        return state.phase

    def expected(self, state: WerewolfState) -> Expectation:
        phase = state.phase
        if phase == "night_wolf":
            wolves = state.living_wolves()
            return Expectation(seats=[wolves[state.wolf_index].no], turn_id=self.turn_id(state))
        if phase == "night_seer":
            seer = next(s for s in state.living() if s.role is rules.Role.SEER)
            return Expectation(seats=[seer.no], turn_id=self.turn_id(state))
        if phase == "night_witch":
            witch = next(s for s in state.living() if s.role is rules.Role.WITCH)
            return Expectation(seats=[witch.no], turn_id=self.turn_id(state))
        if phase == "last_words":
            return Expectation(seats=[state.lw_queue[state.lw_index]], turn_id=self.turn_id(state))
        if phase == "hunter_shot":
            assert state.pending_hunter is not None
            return Expectation(seats=[state.pending_hunter], turn_id=self.turn_id(state))
        if phase == "day_discuss":
            order = state.living_nos()
            return Expectation(seats=[order[state.discuss_index]], turn_id=self.turn_id(state))
        if phase == "day_vote":
            voters = [s.no for s in state.living() if s.can_vote]
            return Expectation(seats=voters, turn_id=self.turn_id(state))
        if phase == "banter":
            return Expectation(seats=[state._banter_order[state.banter_index]], turn_id=self.turn_id(state))
        raise RuntimeError(f"unexpected phase for expected(): {phase}")

    # ---------------------------------------------------------------- view（私密信息隔离的唯一闸口）

    def view(self, seat: int, state: WerewolfState, thread: SeatThread, *, first_speak: bool = False) -> Any:
        expected = self.expected(state)
        if seat not in expected.seats:
            raise ValueError(
                f"seat {seat} 无权读取 turn {expected.turn_id} 的行动视图；"
                f"当前行动席位是 {expected.seats}"
            )
        s = state.seat(seat)
        phase = state.phase
        view = PlayerView(
            seat_no=seat,
            role=s.role,
            players=state.players,
            day=state.day,
            phase=phase,
            alive_nos=state.living_nos(),
            dead_nos=[x.no for x in state.seats if not x.alive],
            wolf_allies=self._wolf_allies(state, s),
            seer_checks=list(state.seer_checks) if s.role is rules.Role.SEER else [],
            witch_has_antidote=state.witch_has_antidote if s.role is rules.Role.WITCH else False,
            witch_has_poison=state.witch_has_poison if s.role is rules.Role.WITCH else False,
            night_target=state.wolf_target if phase == "night_witch" and s.role is rules.Role.WITCH else None,
            candidates=self._candidates(seat, state),
            deaths=[
                PublicDeath(seat_no=d.seat_no, role=d.role, day=d.day)
                for d in sorted(state.deaths, key=lambda item: (item.day, item.seat_no))
            ],
            votes_history=list(state.votes_history),
            tone=s.tone,
            strategy=s.strategy_system,
            messages=list(thread.messages),
        )
        with_situation = phase != "banter"
        mimicry_guard = phase != "banter"
        ask = prompts.opencode_ask(phase, view)
        context = prompts.situation(view) if with_situation else ""
        private = prompts.private_block(view)
        if private:
            context = f"{context}\n{private}" if context else private
        view.turn_prompt = prompts.human_message(
            thread.inbox, context, ask, view.tone, mimicry_guard=mimicry_guard
        )
        view.prompt = view.turn_prompt
        view.prompt = prompts.opencode_prompt(view, include_context=first_speak)
        return view

    def _wolf_allies(self, state: WerewolfState, seat: Seat) -> list[int]:
        """狼人发牌时就知道的队友名单——不随死亡变化（队友死了你仍知道他是队友），
        故不按存活过滤；这样固定设定整局逐字不变、吃 prompt 缓存。"""
        if seat.role is not rules.Role.WEREWOLF:
            return []
        return [s.no for s in state.seats if s.role is rules.Role.WEREWOLF and s.no != seat.no]

    def _candidates(self, seat: int, state: WerewolfState) -> list[int]:
        phase = state.phase
        if phase == "night_wolf":
            return [x.no for x in state.living() if x.role is not rules.Role.WEREWOLF]
        if phase in ("night_seer", "night_witch", "day_vote"):
            return [no for no in state.living_nos() if no != seat]
        if phase == "hunter_shot":
            return state.living_nos()
        return []

    # ---------------------------------------------------------------- parse（唯一懂语义的解析点）

    def parse(self, seat: int, raw: str, state: WerewolfState) -> ParseResult:
        phase = state.phase
        if phase == "night_wolf":
            return self._parse_wolf(seat, raw, state)
        if phase == "night_seer":
            return self._parse_seer(seat, raw, state)
        if phase == "night_witch":
            return self._parse_witch(seat, raw, state)
        if phase in ("last_words", "day_discuss", "banter"):
            return self._parse_statement(phase, seat, raw, state)
        if phase == "day_vote":
            return self._parse_vote(seat, raw, state)
        if phase == "hunter_shot":
            return self._parse_hunter(seat, raw, state)
        raise RuntimeError(f"unexpected phase for parse(): {phase}")

    def _parse_wolf(self, seat: int, raw: str, state: WerewolfState) -> ParseResult:
        result = parse_json(raw, WolfKill)
        candidates = self._candidates(seat, state)
        if result.target_no not in candidates:
            raise JsonParseError(f"狼人 {seat}号 选择了非法刀口 {result.target_no}（可选：{candidates}）")
        others = [w.no for w in state.living_wolves() if w.no != seat]
        deliveries = [
            # 狼队夜聊只进其他存活狼席位的 inbox；观众上帝视角同文可见。好人永不可见。
            Delivery(text=prompts.line_wolf_chat(seat, result.target_no, result.speech), channels=[*others, "audience"]),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "wolf_kill", "seat": seat, "target_no": result.target_no,
            "speech": result.speech, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_seer(self, seat: int, raw: str, state: WerewolfState) -> ParseResult:
        result = parse_json(raw, SeerAction)
        candidates = self._candidates(seat, state)
        if result.target_no not in candidates:
            raise JsonParseError(f"预言家 {seat}号 查验了非法对象 {result.target_no}（可选：{candidates}）")
        deliveries = [
            # 玩家句子只承载预言家的内心戏；裁判反馈在 apply 后由 host 私密投递。
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        target = state.seat(result.target_no)
        action = json.dumps({
            "kind": "seer_check", "seat": seat, "target_no": result.target_no,
            "result": target.side.value, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_witch(self, seat: int, raw: str, state: WerewolfState) -> ParseResult:
        result = parse_json(raw, WitchAction)
        candidates = self._candidates(seat, state)
        if result.save and (not state.witch_has_antidote or state.wolf_target is None):
            raise JsonParseError(f"女巫 {seat}号 非法使用解药（无药或今夜无刀口）")
        if result.poison_target_no is not None and (not state.witch_has_poison or result.poison_target_no not in candidates):
            raise JsonParseError(f"女巫 {seat}号 非法使用毒药：{result.poison_target_no}（可选：{candidates}）")
        if result.save and result.poison_target_no == state.wolf_target:
            raise JsonParseError(f"女巫 {seat}号 同夜对 {state.wolf_target}号 又救又毒")
        deliveries = [Delivery(text=result.monologue, channels=["audience"])]
        action = json.dumps({
            "kind": "witch_action", "seat": seat, "save": result.save,
            "poison_target_no": result.poison_target_no, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_statement(self, phase: str, seat: int, raw: str, state: WerewolfState) -> ParseResult:
        result = parse_json(raw, Statement)
        kind = {"last_words": "last_words", "day_discuss": "discuss", "banter": "banter"}[phase]
        if result.phase != kind:
            raise JsonParseError(f"动作 phase 应为 {kind}，实际为 {result.phase}")
        deliveries = [
            Delivery(text=prompts.line_player(seat, result.speech), channels=["broadcast"]),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": kind, "seat": seat,
            "speech": result.speech, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_vote(self, seat: int, raw: str, state: WerewolfState) -> ParseResult:
        result = parse_json(raw, VoteAction)
        living = set(state.living_nos())
        if result.target_no not in living or result.target_no == seat:
            raise JsonParseError(f"{seat}号 投了非法对象 {result.target_no}（须存活且不能投自己）")
        deliveries = [Delivery(text=result.monologue, channels=["audience"])]
        action = json.dumps({
            "kind": "vote", "seat": seat,
            "target_no": result.target_no, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    def _parse_hunter(self, seat: int, raw: str, state: WerewolfState) -> ParseResult:
        result = parse_json(raw, HunterShot)
        living = set(state.living_nos())
        if result.target_no is not None and result.target_no not in living:
            raise JsonParseError(f"猎人 {seat}号 开枪目标非法：{result.target_no}（可选：{sorted(living)}）")
        deliveries = [
            Delivery(text=prompts.line_player(seat, result.speech), channels=["broadcast"]),
            Delivery(text=result.monologue, channels=["audience"]),
        ]
        action = json.dumps({
            "kind": "hunter_shot", "seat": seat, "target_no": result.target_no,
            "speech": result.speech, "monologue": result.monologue,
        }, ensure_ascii=False)
        return ParseResult(deliveries=deliveries, action=action)

    # ---------------------------------------------------------------- apply（纯 fold）

    def apply(self, state: WerewolfState, action: str) -> WerewolfState:
        a = json.loads(action)
        kind = a["kind"]
        out: dict[str, Any] = {"host_outbox": []}  # 任何玩家行动都消费掉上一批串场词（见模块 docstring）
        if kind == "wolf_kill":
            out["wolf_proposals"] = [*state.wolf_proposals, {"seat": a["seat"], "target": a["target_no"]}]
        elif kind == "seer_check":
            out["seer_checks"] = [*state.seer_checks, SeerCheck(target_no=a["target_no"], side=rules.Side(a["result"]), day=state.day)]
            out["host_outbox"] = [
                _emit(
                    f"day{state.day}.seer_result",
                    f"（查验结果）{a['target_no']}号是{rules.SIDE_LABEL[rules.Side(a['result'])]}。",
                    [a["seat"], "audience"],
                    {
                        "kind": "seer_result",
                        "day": state.day,
                        "seat": a["seat"],
                        "target_no": a["target_no"],
                        "result": a["result"],
                    },
                )
            ]
        elif kind == "witch_action":
            out["witch_saved"] = a["save"]
            out["witch_poison_target"] = a["poison_target_no"]
            out["witch_has_antidote"] = state.witch_has_antidote and not a["save"]
            out["witch_has_poison"] = state.witch_has_poison and a["poison_target_no"] is None
            saved_target = state.wolf_target if a["save"] else None
            save_text = f"救{saved_target}号" if saved_target is not None else "不用解药"
            poison_text = (
                f"毒{a['poison_target_no']}号"
                if a["poison_target_no"] is not None
                else "不用毒药"
            )
            out["host_outbox"] = [
                _audience_event(
                    f"day{state.day}.witch_result",
                    f"【女巫裁决】{save_text}，{poison_text}。",
                    {
                        "kind": "witch_result",
                        "day": state.day,
                        "seat": a["seat"],
                        "wolf_target_no": state.wolf_target,
                        "save": a["save"],
                        "saved_target_no": saved_target,
                        "poison_target_no": a["poison_target_no"],
                        "antidote_remaining": out["witch_has_antidote"],
                        "poison_remaining": out["witch_has_poison"],
                    },
                )
            ]
        elif kind == "vote":
            out["vote_round"] = {**state.vote_round, a["seat"]: a["target_no"]}
        elif kind == "hunter_shot":
            out["hunter_action"] = dict(a)
        elif kind in ("last_words", "discuss", "banter"):
            pass  # 发言不进账本（原文已在句子日志里），只消费 outbox
        else:
            raise RuntimeError(f"unknown action kind: {kind}")
        return state.model_copy(deep=True, update=out)

    # ---------------------------------------------------------------- advance（阶段机 + 串场词产出）

    def advance(self, state: WerewolfState) -> WerewolfState:
        phase = state.phase
        if phase == "night_wolf":
            return self._advance_wolf(state)
        if phase == "night_seer":
            return self._after_seer(state)
        if phase == "night_witch":
            return self._resolve_night(state)
        if phase == "last_words":
            return self._advance_last_words(state)
        if phase == "hunter_shot":
            return self._resolve_hunter(state)
        if phase == "day_discuss":
            if state.discuss_index + 1 < len(state.living_nos()):
                return state.model_copy(deep=True, update={"discuss_index": state.discuss_index + 1, "host_outbox": []})
            return state.model_copy(deep=True, update={"phase": "day_vote", "vote_round": {}, "host_outbox": []})
        if phase == "day_vote":
            return self._resolve_vote(state)
        if phase == "banter":
            if state.banter_index + 1 < state.players:
                return state.model_copy(deep=True, update={"banter_index": state.banter_index + 1, "host_outbox": []})
            closing = _emit("postgame.closing", prompts.line_host_closing(), ["broadcast", "audience"], {"kind": "closing"})
            return state.model_copy(deep=True, update={"phase": "done", "host_outbox": [closing]})
        if phase == "done":
            return state
        raise RuntimeError(f"unexpected phase for advance(): {phase}")

    def _advance_wolf(self, state: WerewolfState) -> WerewolfState:
        wolves = state.living_wolves()
        if state.wolf_index + 1 < len(wolves):
            return state.model_copy(deep=True, update={"wolf_index": state.wolf_index + 1, "host_outbox": []})
        # 全体存活狼已提议：刀口取多数，平票取最小座位号。
        counts = Counter(p["target"] for p in state.wolf_proposals)
        target = min((-n, no) for no, n in counts.items())[1]
        proposals = [
            {"seat": proposal["seat"], "target_no": proposal["target"]}
            for proposal in state.wolf_proposals
        ]
        marks = "，".join(
            f"{proposal['seat']}号→{proposal['target_no']}号" for proposal in proposals
        )
        result = _audience_event(
            f"day{state.day}.wolf_kill_result",
            f"【狼刀裁决】提议[{marks}]，最终刀口为{target}号。",
            {
                "kind": "wolf_kill_result",
                "day": state.day,
                "proposals": proposals,
                "target_no": target,
                "resolution": "majority_then_lowest_seat",
            },
        )
        targeted = state.model_copy(deep=True, update={"wolf_target": target})
        return self._after_night_actor(targeted, (result,))

    def _after_seer(self, state: WerewolfState) -> WerewolfState:
        return self._after_night_actor(state)

    def _after_night_actor(
        self,
        state: WerewolfState,
        announces: tuple[HostEmit, ...] = (),
    ) -> WerewolfState:
        """狼刀/预言家之后：还有夜间行动者就去下一个，否则直接结算夜晚。"""
        witch_alive = any(s.role is rules.Role.WITCH for s in state.living())
        seer_alive = any(s.role is rules.Role.SEER for s in state.living())
        if state.phase == "night_wolf" and seer_alive:
            return state.model_copy(
                deep=True,
                update={"phase": "night_seer", "host_outbox": list(announces)},
            )
        if state.phase in ("night_wolf", "night_seer") and witch_alive:
            return state.model_copy(
                deep=True,
                update={"phase": "night_witch", "host_outbox": list(announces)},
            )
        return self._resolve_night(state, announces)

    def _resolve_night(
        self,
        state: WerewolfState,
        announces: tuple[HostEmit, ...] = (),
    ) -> WerewolfState:
        """夜晚结算（纯计算）：算出今夜死者、定下待开枪的猎人，天亮播报交给 pending。"""
        deaths: list[DeathRecord] = []
        if state.wolf_target is not None and not state.witch_saved:
            t = state.seat(state.wolf_target)
            deaths.append(DeathRecord(seat_no=t.no, role=t.role, reason="wolf_kill", day=state.day))
        if state.witch_poison_target is not None:
            t = state.seat(state.witch_poison_target)
            deaths.append(DeathRecord(seat_no=t.no, role=t.role, reason="poison", day=state.day))
        # 同一人被刀又被毒只记一次，并保留先入的狼刀死因；这是猎人仍可开枪的规则依据。
        seen: set[int] = set()
        deaths = [d for d in deaths if not (d.seat_no in seen or seen.add(d.seat_no))]
        seats = self._apply_deaths(state, deaths)
        # 死于狼刀的猎人可开枪，死于毒不可
        hunter = next((d.seat_no for d in deaths if d.role is rules.Role.HUNTER and d.reason != "poison"), None)
        public_deaths = sorted(deaths, key=lambda item: item.seat_no)
        dawn = _emit(
            f"day{state.day}.dawn",
            prompts.line_host_dawn(
                state.day,
                [(death.seat_no, death.role) for death in public_deaths],
            ),
            ["broadcast", "audience"],
            {
                "kind": "dawn",
                "day": state.day,
                "deaths": [
                    {
                        "seat_no": death.seat_no,
                        "role": death.role.value,
                        "day": death.day,
                    }
                    for death in public_deaths
                ],
            },
        )
        resolved = state.model_copy(deep=True, update={
            "seats": seats,
            "deaths": [*state.deaths, *deaths],
            "round_deaths": deaths,
            "pending_hunter": hunter,
            "wolf_target": None,
            "witch_saved": False,
            "witch_poison_target": None,
            "wolf_proposals": [],
            "wolf_index": 0,
            "host_outbox": [*announces, dawn],
        })
        return self._after_deaths(resolved, origin="night")

    def _advance_last_words(self, state: WerewolfState) -> WerewolfState:
        if state.lw_index + 1 < len(state.lw_queue):
            return state.model_copy(deep=True, update={"lw_index": state.lw_index + 1, "host_outbox": []})
        if state.pending_hunter is not None:
            return state.model_copy(deep=True, update={"phase": "hunter_shot", "host_outbox": []})
        return self._gate(state)

    def _resolve_hunter(self, state: WerewolfState) -> WerewolfState:
        """猎人开枪结算：命中者当场翻牌、加入本轮死者；随后判胜负。"""
        a = state.hunter_action
        assert a is not None and state.pending_hunter is not None
        hunter_no = state.pending_hunter
        deaths: list[DeathRecord] = []
        seats = state.seats
        victim_role: rules.Role | None = None
        if a["target_no"] is not None:
            victim = state.seat(a["target_no"])
            victim_role = victim.role
            death = DeathRecord(seat_no=victim.no, role=victim.role, reason="hunter_shot", day=state.day)
            seats = self._apply_deaths(state, [death])
            deaths = [death]
        shot = _emit(
            f"day{state.day}.hunter_result",
            prompts.line_hunter_shot(hunter_no, a["target_no"], victim_role),
            ["broadcast", "audience"],
            {
                "kind": "hunter_shot_result",
                "hunter": hunter_no,
                "target_no": a["target_no"],
                "victim_role": victim_role.value if victim_role else None,
            },
        )
        resolved = state.model_copy(deep=True, update={
            "seats": seats,
            "deaths": [*state.deaths, *deaths],
            "round_deaths": [*state.round_deaths, *deaths],
            "pending_hunter": None,
            "hunter_action": None,
            "host_outbox": [shot],
        })
        return self._gate(resolved)

    def _resolve_vote(self, state: WerewolfState) -> WerewolfState:
        """放逐结算：公布票型与结果。白痴首次被放逐翻牌免死、失投票权；已翻牌白痴再被投
        则放逐无效；其余正常出局翻牌、若是猎人则待开枪。平票无人出局。"""
        votes = dict(state.vote_round)
        counts = Counter(votes.values())
        top = max(counts.values()) if counts else 0
        leaders = sorted(no for no, n in counts.items() if n == top)
        tied = len(leaders) != 1
        top_voted = None if tied else leaders[0]
        top_role = state.seat(top_voted).role if top_voted is not None else None
        # 白痴无论首次翻牌还是已经翻牌都不会被放逐；账本必须区分“最高票”和“实际出局”。
        exiled = (
            top_voted
            if top_voted is not None and top_role is not rules.Role.IDIOT
            else None
        )
        record = VoteRecord(
            day=state.day,
            votes=votes,
            top_voted=top_voted,
            exiled=exiled,
            tied=tied,
        )
        base_update: dict[str, Any] = {"votes_history": [*state.votes_history, record]}
        # 白痴翻牌免死的局，结果播报只说票型、不说"出局"（免死播报紧随其后，先报出局自相矛盾）。
        announces = [
            HostEmit(
                turn_id=f"day{state.day}.vote_result",
                deliveries=[
                    Delivery(
                        text=prompts.line_vote_detail(votes),
                        channels=["broadcast", "audience"],
                        spoken=False,
                    ),
                    Delivery(
                        text=prompts.line_host_vote_result(top_voted, top_role, tied),
                        channels=["broadcast", "audience"],
                    ),
                ],
                action=json.dumps(
                    {
                        "kind": "vote_result",
                        "day": state.day,
                        "votes": votes,
                        "top_voted": top_voted,
                        "exiled": exiled,
                        "tied": tied,
                    },
                    ensure_ascii=False,
                ),
            ),
        ]
        if top_voted is None:
            return self._enter_night(state.model_copy(deep=True, update=base_update), announces)
        target = state.seat(top_voted)
        if target.role is rules.Role.IDIOT:
            if not target.revealed:
                seats = [s.model_copy(update={"revealed": True, "can_vote": False}) if s.no == target.no else s for s in state.seats]
                announces.append(_emit(
                    f"day{state.day}.idiot",
                    prompts.line_host_idiot(target.no),
                    ["broadcast", "audience"],
                    {"kind": "idiot_revealed", "day": state.day, "seat_no": target.no},
                ))
                return self._enter_night(state.model_copy(deep=True, update={**base_update, "seats": seats}), announces)
            announces.append(_emit(
                f"day{state.day}.idiot",
                prompts.line_idiot_immune(target.no),
                ["broadcast", "audience"],
                {"kind": "idiot_immune", "day": state.day, "seat_no": target.no},
            ))
            return self._enter_night(state.model_copy(deep=True, update=base_update), announces)
        death = DeathRecord(seat_no=target.no, role=target.role, reason="exile", day=state.day)
        seats = self._apply_deaths(state, [death])
        hunter = target.no if target.role is rules.Role.HUNTER else None
        resolved = state.model_copy(deep=True, update={
            **base_update,
            "seats": seats,
            "deaths": [*state.deaths, death],
            "round_deaths": [death],
            "pending_hunter": hunter,
            "host_outbox": announces,
        })
        return self._after_deaths(resolved, origin="vote")

    # ---------------------------------------------------------------- 阶段机共享部件

    def _apply_deaths(self, state: WerewolfState, deaths: list[DeathRecord]) -> list[Seat]:
        dead = {d.seat_no for d in deaths}
        return [
            s.model_copy(update={"alive": False, "can_vote": False, "revealed": True}) if s.no in dead else s
            for s in state.seats
        ]

    def _after_deaths(self, state: WerewolfState, *, origin: str) -> WerewolfState:
        """死亡链入口：遗言（不含待开枪猎人）→ 猎人开枪 → 判胜负。host_outbox 保留已有播报。"""
        lw_queue = sorted(
            death.seat_no
            for death in state.round_deaths
            if death.seat_no != state.pending_hunter
        )
        if lw_queue:
            return state.model_copy(deep=True, update={
                "phase": "last_words", "lw_queue": lw_queue, "lw_index": 0, "origin": origin,
            })
        if state.pending_hunter is not None:
            return state.model_copy(deep=True, update={"phase": "hunter_shot", "lw_queue": [], "lw_index": 0, "origin": origin})
        return self._gate(state.model_copy(deep=True, update={"lw_queue": [], "lw_index": 0, "origin": origin}))

    def _gate(self, state: WerewolfState) -> WerewolfState:
        """死亡链尽头：判胜负。未分胜负则按来源去白天发言（夜）或下一夜（放逐）。"""
        winner, reason = self._win(state)
        if winner is not None:
            roles = {s.no: s.role for s in state.seats}
            reveal = _emit(
                "postgame.reveal",
                prompts.line_host_reveal(winner, reason, roles),
                ["broadcast", "audience"],
                {
                    "kind": "reveal",
                    "winner": winner,
                    "win_reason": reason,
                    "roles": {no: r.value for no, r in roles.items()},
                    "deaths": [
                        death.model_dump(mode="json")
                        for death in sorted(
                            state.deaths,
                            key=lambda item: (item.day, item.seat_no),
                        )
                    ],
                },
            )
            return state.model_copy(deep=True, update={
                "winner": winner, "win_reason": reason,
                "phase": "banter", "banter_index": 0,
                "host_outbox": [*state.host_outbox, reveal],
            })
        if state.origin == "night":
            return state.model_copy(deep=True, update={"phase": "day_discuss", "discuss_index": 0})
        return self._enter_night(state, list(state.host_outbox))

    def _enter_night(self, state: WerewolfState, announces: list[HostEmit]) -> WerewolfState:
        """进入下一夜：day+1，重置夜晚与投票的临时账本，主持人报天黑。"""
        day = state.day + 1
        night = _emit(
            f"day{day}.night_start",
            prompts.line_host_night(day),
            ["broadcast", "audience"],
            {"kind": "night_start", "day": day},
        )
        return state.model_copy(deep=True, update={
            "day": day,
            "phase": "night_wolf",
            "round_deaths": [],
            "pending_hunter": None,
            "hunter_action": None,
            "wolf_target": None,
            "wolf_proposals": [],
            "wolf_index": 0,
            "witch_saved": False,
            "witch_poison_target": None,
            "vote_round": {},
            "lw_queue": [],
            "lw_index": 0,
            "origin": "night",
            "host_outbox": [*announces, night],
        })

    def _win(self, state: WerewolfState) -> tuple[str | None, str | None]:
        """屠边判负。同一次结算同时达成双方条件时，狼全灭的好人胜优先。"""
        pairs = [(s.role, s.alive) for s in state.seats]
        if rules.living_wolves(pairs) == 0:
            return "good", "all_wolves_dead"
        if rules.living_gods(pairs) == 0:
            return "werewolf", "all_gods_dead"
        if rules.living_villagers(pairs) == 0:
            return "werewolf", "all_villagers_dead"
        return None, None

    def _seats_payload(self, seats: list[Seat]) -> list[dict[str, Any]]:
        return [
            {"no": s.no, "persona": s.persona_id, "strategy": s.strategy_id, "role": s.role.value, "side": s.side.value, "model": s.model}
            for s in seats
        ]

    # ---------------------------------------------------------------- pending（host 串场词，纯查询）

    def pending(self, state: WerewolfState) -> list[HostEmit]:
        # outbox 由 apply/advance 显式重写，loop 的两次 flush 不会重发（见模块 docstring）。
        return list(state.host_outbox)

    # ---------------------------------------------------------------- random sampler（零网络）

    def random_action(self, seat: int, view: PlayerView) -> str:
        """零网络 sampler：为当前 phase 生成合法 JSON raw。"""
        phase = view.phase
        r = random.Random(f"{seat}:{view.day}:{phase}")

        if phase == "night_wolf":
            target = r.choice(view.candidates)
            return WolfKill(
                phase="wolf_kill",
                target_no=target,
                speech=f"（随机）我建议刀{target}号，没意见就定了。",
                monologue="（随机）闭眼点刀。",
            ).model_dump_json()
        if phase == "night_seer":
            return SeerAction(
                phase="seer_check",
                target_no=r.choice(view.candidates),
                monologue="（随机）随便验一个。",
            ).model_dump_json()
        if phase == "night_witch":
            save = view.witch_has_antidote and view.night_target is not None and r.random() < 0.3
            poison = None
            if view.witch_has_poison and r.random() < 0.2:
                poison = r.choice(view.candidates)
                if save and poison == view.night_target:
                    poison = None  # 不同时又救又毒
            return WitchAction(
                phase="witch_action",
                save=save,
                poison_target_no=poison,
                monologue="（随机）药瓶在手，心里不慌。",
            ).model_dump_json()
        if phase == "last_words":
            return Statement(
                phase="last_words",
                speech="（随机）我走了，好人自己看着办吧。",
                monologue="（随机）下班了。",
            ).model_dump_json()
        if phase == "day_discuss":
            return Statement(
                phase="discuss",
                speech="（随机）我暂时看不出谁像狼，先听听后面的。",
                monologue="（随机）水一句，别看我。",
            ).model_dump_json()
        if phase == "day_vote":
            return VoteAction(
                phase="vote",
                target_no=r.choice(view.candidates),
                monologue="（随机）抛硬币定的。",
            ).model_dump_json()
        if phase == "hunter_shot":
            target = r.choice(view.candidates) if r.random() < 0.7 else None
            line = f"（随机）我带走{target}号。" if target is not None else "（随机）我不开枪，体面离场。"
            return HunterShot(
                phase="hunter_shot",
                target_no=target,
                speech=line,
                monologue="（随机）枪随缘。",
            ).model_dump_json()
        if phase == "banter":
            return Statement(
                phase="banter",
                speech="（随机）这局打完了，各位辛苦，下把再战。",
                monologue="（随机）复盘就免了，输赢全靠脸。",
            ).model_dump_json()
        raise ValueError(f"未知决策阶段：{phase}")
