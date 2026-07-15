"""阿瓦隆的最小 Rules 实现（stub），用于验证新 sentence runtime 能跑通真实玩法形状。

不是完整 Avalon：只实现 5 人局、固定角色、第一任务、无刺杀/湖中仙女。
但覆盖了 night reveal、轮流发言、组队提案、同时表决、任务出牌、结果公布
等关键拓扑（private / broadcast / audience / concurrent）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from avalon.rules import fails_required, night_intel, team_size
from avalon.state import Role, Side
from core.sr.rules import Rules
from core.sr.types import Delivery, Expectation, HostEmit, ParseResult

PLAYERS = 5

# 固定角色，确保回放/测试结果稳定。1=MERLIN, 2/3=SERVANT, 4=ASSASSIN, 5=MINION
_ROLES = [Role.MERLIN, Role.SERVANT, Role.SERVANT, Role.ASSASSIN, Role.MINION]


class AvalonStubRules(Rules):
    """阿瓦隆最小规则：5 人固定角色，第一任务，无刺杀。"""

    def initial_state(self, setup: Any) -> dict:
        roles = {i + 1: r for i, r in enumerate(_ROLES)}
        return {
            "players": PLAYERS,
            "roles": roles,
            "phase": "speeches",
            "step": 0,
            "leader": 1,
            "team": [],
            "votes": {},
            "cards": {},
            "results": [],
            "transcript": [],
            "revealed": False,
        }

    def is_terminal(self, state: dict) -> bool:
        return state["phase"] == "done"

    def turn_id(self, state: dict) -> str:
        if state["phase"] == "speeches":
            return f"avalon.speeches.{state['step']}"
        return f"avalon.{state['phase']}"

    def expected(self, state: dict) -> Expectation:
        if state["phase"] == "speeches":
            return Expectation(seats=[state["step"] + 1], turn_id=self.turn_id(state))
        if state["phase"] == "propose":
            return Expectation(seats=[state["leader"]], turn_id=self.turn_id(state))
        if state["phase"] == "vote":
            return Expectation(seats=list(range(1, PLAYERS + 1)), turn_id=self.turn_id(state))
        if state["phase"] == "quest":
            return Expectation(seats=state["team"], turn_id=self.turn_id(state))
        raise RuntimeError(f"unexpected phase for expected(): {state['phase']}")

    def view(self, seat: int, state: dict, inbox: list[str], *, first_speak: bool = False) -> Any:
        role = state["roles"][seat]
        if state["phase"] == "speeches":
            prompt = f"你是 {seat} 号玩家，角色是 {role.value}。请做一次桌面发言。"
        elif state["phase"] == "propose":
            q = len(state["results"]) + 1
            size = team_size(PLAYERS, q)
            prompt = f"你是 {seat} 号队长，请为第 {q} 个任务选择 {size} 人上队伍。"
        elif state["phase"] == "vote":
            prompt = f"当前队伍是 {state['team']}，请投票赞成或反对。"
        elif state["phase"] == "quest":
            prompt = "你在任务队伍里，请出任务牌（成功/失败）。"
        else:
            prompt = "请发言。"
        return SimpleNamespace(prompt=prompt)

    def parse(self, seat: int, raw: str, state: dict) -> ParseResult:
        phase = state["phase"]
        if phase == "speeches":
            return ParseResult(
                deliveries=[Delivery(text=raw, channels=["broadcast"])],
                action=json.dumps({"kind": "speech", "seat": seat, "text": raw}, ensure_ascii=False),
            )
        if phase == "propose":
            # 固定提案：队长 + 末席（红方），确保任务会被破坏
            leader = state["leader"]
            team = [leader, PLAYERS]
            return ParseResult(
                deliveries=[],  # 主持人统一在 pending 中公布队伍，避免重复
                action=json.dumps({"kind": "propose", "team": team}, ensure_ascii=False),
            )
        if phase == "vote":
            role = state["roles"][seat]
            approve = role.side is Side.GOOD
            text = f"{seat} 号玩家投票 {'赞成' if approve else '反对'}。"
            return ParseResult(
                deliveries=[Delivery(text=text, channels=["audience"])],
                action=json.dumps({"kind": "vote", "seat": seat, "approve": approve}, ensure_ascii=False),
            )
        if phase == "quest":
            role = state["roles"][seat]
            success = role.side is Side.GOOD
            text = f"{seat} 号玩家已出任务牌。"
            return ParseResult(
                deliveries=[Delivery(text=text, channels=["audience"])],
                action=json.dumps({"kind": "quest", "seat": seat, "success": success}, ensure_ascii=False),
            )
        raise RuntimeError(f"unexpected phase for parse(): {phase}")

    def apply(self, state: dict, action: str) -> dict:
        a = json.loads(action)
        kind = a["kind"]
        if kind == "speech":
            return {**state, "transcript": [*state["transcript"], a["text"]], "revealed": True}
        if kind == "propose":
            return {**state, "team": a["team"]}
        if kind == "vote":
            return {**state, "votes": {**state["votes"], a["seat"]: a["approve"]}}
        if kind == "quest":
            return {**state, "cards": {**state["cards"], a["seat"]: a["success"]}}
        raise RuntimeError(f"unknown action kind: {kind}")

    def advance(self, state: dict) -> dict:
        phase = state["phase"]
        if phase == "speeches":
            step = state["step"] + 1
            if step >= PLAYERS:
                return {**state, "phase": "propose", "step": step}
            return {**state, "step": step}
        if phase == "propose":
            return {**state, "phase": "vote"}
        if phase == "vote":
            return {**state, "phase": "quest"}
        if phase == "quest":
            cards = state["cards"]
            fails = sum(1 for success in cards.values() if not success)
            q = len(state["results"]) + 1
            req = fails_required(PLAYERS, q)
            result = "fail" if fails >= req else "success"
            return {**state, "phase": "done", "results": [*state["results"], result]}
        if phase == "done":
            return state
        raise RuntimeError(f"unexpected phase for advance(): {phase}")

    def pending(self, state: dict) -> list[HostEmit]:
        phase = state["phase"]
        if phase == "speeches" and not state["revealed"]:
            return self._night_reveals(state)
        if phase == "vote" and not state["votes"]:
            # 队伍提案已确定，即将进入投票；公布队伍
            team = state["team"]
            return [
                HostEmit(
                    turn_id="avalon.host.propose",
                    deliveries=[Delivery(text=f"主持人：队长提案队伍为 {team}。", channels=["broadcast", "audience"])],
                    action=json.dumps({"kind": "propose_announce", "team": team}, ensure_ascii=False),
                )
            ]
        if phase == "quest" and not state["cards"]:
            # 投票刚结束，公布投票结果
            votes = state["votes"]
            approve = sum(1 for v in votes.values() if v)
            reject = len(votes) - approve
            approved = approve > reject
            return [
                HostEmit(
                    turn_id="avalon.host.vote_result",
                    deliveries=[
                        Delivery(
                            text=f"主持人：投票结果 {approve} 赞成 {reject} 反对，队伍{'通过' if approved else '被否决'}。",
                            channels=["broadcast", "audience"],
                        )
                    ],
                    action=json.dumps({"kind": "vote_result", "votes": votes, "approved": approved}, ensure_ascii=False),
                )
            ]
        if phase == "done":
            result = state["results"][-1]
            return [
                HostEmit(
                    turn_id="avalon.host.quest_result",
                    deliveries=[
                        Delivery(
                            text=f"主持人：第 {len(state['results'])} 个任务 { '成功' if result == 'success' else '失败' }。",
                            channels=["broadcast", "audience"],
                        )
                    ],
                    action=json.dumps({"kind": "quest_result", "result": result}, ensure_ascii=False),
                )
            ]
        return []

    def _night_reveals(self, state: dict) -> list[HostEmit]:
        roles = [state["roles"][i] for i in range(1, PLAYERS + 1)]
        deliveries: list[Delivery] = []
        for seat in range(1, PLAYERS + 1):
            intel = night_intel(roles, seat)
            if intel:
                deliveries.append(
                    Delivery(
                        text=f"主持人（私）：你的夜晚情报是 {intel}。",
                        channels=[seat],
                    )
                )
        deliveries.append(
            Delivery(text="主持人：夜晚阶段结束，天亮了。", channels=["broadcast", "audience"])
        )
        return [
            HostEmit(
                turn_id="avalon.host.night_reveal",
                deliveries=deliveries,
                action=json.dumps({"kind": "night_reveal"}, ensure_ascii=False),
            )
        ]

    def pack(self, state: dict) -> bytes:
        return json.dumps(state, ensure_ascii=False).encode("utf-8")

    def unpack(self, raw: bytes) -> dict:
        return json.loads(raw.decode("utf-8"))
