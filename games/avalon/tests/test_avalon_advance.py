"""AvalonRules 阶段机推进：计票、否决上限、任务轨道、刺杀入口、pending 单发纪律。"""

from __future__ import annotations

import json

from avalon import rules as av_rules
from avalon.registry import load_registry
from avalon.rules_engine import AvalonRules, AvalonState
from avalon.state import Role, Seat

_ROLES = [Role.MERLIN, Role.PERCIVAL, Role.SERVANT, Role.ASSASSIN, Role.MORGANA]


def _rules() -> AvalonRules:
    return AvalonRules(registry=load_registry(), settings=None)


def _state(**over) -> AvalonState:
    seats = [
        Seat(no=i + 1, persona_id="fox", strategy_id="prudent", model="", role=role, intel=av_rules.night_intel(_ROLES, i + 1))
        for i, role in enumerate(_ROLES)
    ]
    st = AvalonState(seats=seats, leader=1)
    return st.model_copy(deep=True, update=over)


def _vote_all(r: AvalonRules, st: AvalonState, approve: bool) -> AvalonState:
    """全员按 approve 表决（5 人局：3 赞成即通过）。"""
    approves = [1, 2, 3] if approve else [1]
    for no in range(1, 6):
        st = r.apply(st, json.dumps({"kind": "team_vote", "seat": no, "approve": no in approves}))
    return st


def test_rejected_proposal_rotates_leader() -> None:
    r = _rules()
    st = _state(phase="team_vote", team=[1, 2], pregame_done=True)
    st = r.advance(_vote_all(r, st, approve=False))
    assert st.phase == "propose" and st.leader == 2 and st.rejections == 1
    assert st.team == [] and st.votes == {}
    # 被否提案进公开账本（旧引擎行为）：队伍/票型可查、result 为空
    assert len(st.history) == 1
    rec = st.history[0]
    assert rec.approved is False and rec.result is None and rec.team == [1, 2] and rec.leader_no == 1


def test_five_rejections_evil_wins() -> None:
    r = _rules()
    st = _state(phase="team_vote", team=[1, 2], pregame_done=True, rejections=4)
    st = r.advance(_vote_all(r, st, approve=False))
    assert st.winner == "evil" and st.win_reason == "rejections"
    assert st.phase == "banter"


def test_approved_team_goes_to_quest() -> None:
    r = _rules()
    st = _state(phase="team_vote", team=[1, 2], pregame_done=True)
    st = r.advance(_vote_all(r, st, approve=True))
    assert st.phase == "quest" and st.cards == {}


def test_three_failed_quests_evil_wins() -> None:
    r = _rules()
    st = _state(phase="quest", team=[1, 4], pregame_done=True)
    # 已败 2 个任务，再败 1 个
    from avalon.state import ProposalRecord
    history = [
        ProposalRecord(quest=i, attempt=1, leader_no=1, team=[1, 4], votes={}, approved=True, fails=1, result="fail")
        for i in (1, 2)
    ]
    st = st.model_copy(update={"history": history})
    st = r.apply(st, json.dumps({"kind": "quest", "seat": 1, "success": True}))
    st = r.apply(st, json.dumps({"kind": "quest", "seat": 4, "success": False}))
    st = r.advance(st)
    assert st.winner == "evil" and st.win_reason == "quests"
    assert st.phase == "banter"


def test_three_succeeded_quests_triggers_assassination() -> None:
    r = _rules()
    from avalon.state import ProposalRecord
    history = [
        ProposalRecord(quest=i, attempt=1, leader_no=1, team=[1, 2], votes={}, approved=True, fails=0, result="success")
        for i in (1, 2)
    ]
    st = _state(phase="quest", team=[1, 2], pregame_done=True).model_copy(update={"history": history})
    st = r.apply(st, json.dumps({"kind": "quest", "seat": 1, "success": True}))
    st = r.apply(st, json.dumps({"kind": "quest", "seat": 2, "success": True}))
    st = r.advance(st)
    assert st.phase == "assassinate"
    assert st.winner is None  # 刺杀前胜负未定
    exp = r.expected(st)
    assert exp.seats == [4]  # 只有刺客行动


def test_quest_result_resets_rejections_and_rotates_leader() -> None:
    r = _rules()
    st = _state(phase="quest", team=[1, 2], pregame_done=True, rejections=3)
    st = r.apply(st, json.dumps({"kind": "quest", "seat": 1, "success": True}))
    st = r.apply(st, json.dumps({"kind": "quest", "seat": 2, "success": True}))
    st = r.advance(st)
    assert st.rejections == 0 and st.leader == 2 and st.phase == "propose"


def test_pending_single_emission_discipline() -> None:
    """开票/任务结果只播报一次：apply 完成后 pending 出一批，advance 之后不再重复。"""
    r = _rules()
    st = _state(phase="team_vote", team=[1, 2], pregame_done=True)
    st = _vote_all(r, st, approve=True)
    batch = r.pending(st)
    assert len(batch) == 1 and json.loads(batch[0].action)["kind"] == "vote_result"
    st = r.advance(st)
    assert all(json.loads(e.action)["kind"] != "vote_result" for e in r.pending(st))
    # 任务出完牌：quest_result 恰好一次
    st = r.apply(st, json.dumps({"kind": "quest", "seat": 1, "success": True}))
    assert r.pending(st) == []
    st = r.apply(st, json.dumps({"kind": "quest", "seat": 2, "success": True}))
    batch = r.pending(st)
    assert len(batch) == 1 and json.loads(batch[0].action)["kind"] == "quest_result"


def test_pregame_opening_emitted_once() -> None:
    r = _rules()
    st = _state()
    batch = r.pending(st)
    assert len(batch) == 1 and json.loads(batch[0].action)["kind"] == "game_start"
    # 首个行动 apply 后开场播报不再重复
    st = r.apply(st, json.dumps({"kind": "propose", "seat": 1, "team": [1, 2], "speech": "s", "monologue": "m"}))
    assert all(json.loads(e.action)["kind"] != "game_start" for e in r.pending(st))


def test_pack_unpack_roundtrip() -> None:
    r = _rules()
    st = _state(phase="quest", team=[1, 4], cards={1: True}, rejections=2)
    assert r.unpack(r.pack(st)) == st


def test_banter_order_losers_first() -> None:
    r = _rules()
    st = _state(phase="banter", winner="evil")
    order = [st.banter_index]
    exp = r.expected(st)
    # 败方（蓝方座位 1~3）先于胜方（红方 4~5）
    assert exp.seats[0] in (1, 2, 3)
    full = [st._banter_order[i] for i in range(5)]
    assert full == [1, 2, 3, 4, 5]
    assert order == [0]
