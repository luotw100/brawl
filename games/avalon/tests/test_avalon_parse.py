"""AvalonRules.parse 的硬校验与信息隔离。

非法产出（坏队伍、蓝方出破坏票、刺错阵营、查验非法对象）一律 JsonParseError，交给
loop 重试，绝不静默上桌。隔离不变量：任务出牌/组队表决的产出只进观众流；梅林看到的
红方名单绝不进其他席位的 view。
"""

from __future__ import annotations

import json

import pytest

from avalon.registry import load_registry
from avalon.rules_engine import AvalonRules, AvalonState
from avalon.state import Role, Seat
from core.parsing import JsonParseError

# 5 人局：1 梅林，2 派西维尔，3 忠臣，4 刺客，5 莫甘娜（蓝 3 红 2）
_ROLES = [Role.MERLIN, Role.PERCIVAL, Role.SERVANT, Role.ASSASSIN, Role.MORGANA]


def _rules() -> AvalonRules:
    return AvalonRules(registry=load_registry(), settings=None)


def _state(**over) -> AvalonState:
    from avalon import rules as av_rules

    seats = [
        Seat(no=i + 1, persona_id="fox", strategy_id="prudent", model="", role=role, intel=av_rules.night_intel(_ROLES, i + 1))
        for i, role in enumerate(_ROLES)
    ]
    st = AvalonState(seats=seats, leader=1)
    return st.model_copy(deep=True, update=over)


def _proposal(team: list[int]) -> str:
    return json.dumps({"team": team, "speech": "听我的", "monologue": "m"}, ensure_ascii=False)


# ---------------------------------------------------------------- 组队硬校验

def test_propose_wrong_size_rejected() -> None:
    st = _state(phase="propose")  # 5 人局 M1 需 2 人
    with pytest.raises(JsonParseError):
        _rules().parse(1, _proposal([1, 2, 3]), st)


def test_propose_duplicate_members_rejected() -> None:
    st = _state(phase="propose")
    with pytest.raises(JsonParseError):
        _rules().parse(1, _proposal([2, 2]), st)


def test_propose_out_of_range_rejected() -> None:
    st = _state(phase="propose")
    with pytest.raises(JsonParseError):
        _rules().parse(1, _proposal([1, 9]), st)


def test_propose_legal_team_accepted() -> None:
    st = _state(phase="propose")
    pr = _rules().parse(1, _proposal([1, 3]), st)
    action = json.loads(pr.action)
    assert action["team"] == [1, 3]
    # 组队宣言广播全员；内心独白只进观众流
    channels = [d.channels for d in pr.deliveries]
    assert ["broadcast"] in channels and ["audience"] in channels


# ---------------------------------------------------------------- 任务出牌

def test_good_side_cannot_play_fail() -> None:
    st = _state(phase="quest", team=[1, 4])
    raw = json.dumps({"success": False, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError, match="蓝方"):
        _rules().parse(1, raw, st)


def test_evil_side_may_play_fail() -> None:
    st = _state(phase="quest", team=[1, 4])
    raw = json.dumps({"success": False, "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(4, raw, st)
    assert json.loads(pr.action)["success"] is False


def test_quest_card_never_reaches_seats() -> None:
    """谁出了什么牌永远保密：任务出牌产出只进观众流。"""
    st = _state(phase="quest", team=[1, 4])
    raw = json.dumps({"success": True, "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(1, raw, st)
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {"audience"}


def test_team_vote_never_reaches_seats_before_tally() -> None:
    """亮票前票型保密：表决产出只进观众流（票型由 host 开票句公布）。"""
    st = _state(phase="team_vote", team=[1, 4])
    raw = json.dumps({"approve": True, "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(2, raw, st)
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {"audience"}


# ---------------------------------------------------------------- 刺杀

def test_assassin_cannot_target_known_teammate() -> None:
    st = _state(phase="assassinate")
    raw = json.dumps({"target_no": 5, "speech": "就是你了", "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError, match="非法目标"):
        _rules().parse(4, raw, st)


def test_assassin_may_stab_oberon_by_mistake() -> None:
    """奥伯伦不参与红方互认：刺客候选清单里有他，误刀合法（等于空刀，蓝方胜）。

    回归：候选清单与校验必须同源——此前校验只收蓝方，奥伯伦在候选里却被拒收，
    RandomActor 固定选中后连错 5 次、整局中断。
    """
    from avalon.state import Seat as _Seat

    roles = [Role.MERLIN, Role.SERVANT, Role.SERVANT, Role.ASSASSIN, Role.OBERON, Role.MORDRED, Role.MINION]
    seats = [
        _Seat(no=i + 1, persona_id="fox", strategy_id="prudent", model="", role=r, intel=[])
        for i, r in enumerate(roles)
    ]
    st = AvalonState(seats=seats, leader=1, phase="assassinate")
    assassin_intel = {4: [6, 7]}  # 刺客夜里只认莫德雷德与爪牙，不认奥伯伦
    st = st.model_copy(update={
        "seats": [s.model_copy(update={"intel": assassin_intel.get(s.no, [])}) for s in st.seats]
    })
    raw = json.dumps({"target_no": 5, "speech": "就是你了", "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(4, raw, st)
    st2 = _rules().apply(st, pr.action)
    assert json.loads(pr.action)["hit"] is False
    assert st2.winner == "good"  # 误刀奥伯伦=空刀，蓝方胜


def test_assassin_hitting_merlin_flips_winner() -> None:
    r = _rules()
    st = _state(phase="assassinate")
    raw = json.dumps({"target_no": 1, "speech": "就是你了", "monologue": "m"}, ensure_ascii=False)
    pr = r.parse(4, raw, st)
    st2 = r.apply(st, pr.action)
    assert json.loads(pr.action)["hit"] is True
    assert st2.winner == "evil" and st2.win_reason == "assassination"


def test_assassin_missing_merlin_good_wins() -> None:
    r = _rules()
    st = _state(phase="assassinate")
    raw = json.dumps({"target_no": 3, "speech": "就是你了", "monologue": "m"}, ensure_ascii=False)
    pr = r.parse(4, raw, st)
    st2 = r.apply(st, pr.action)
    assert json.loads(pr.action)["hit"] is False
    assert st2.winner == "good"


# ---------------------------------------------------------------- 湖中仙女

def test_lady_cannot_recheck_previous_holder() -> None:
    st = _state(phase="lady_choose", lady_holder=2, lady_used=[2, 4])
    raw = json.dumps({"target_no": 4, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(2, raw, st)


def test_lady_choice_reveals_side_to_holder_only() -> None:
    r = _rules()
    st = _state(phase="lady_choose", lady_holder=2, lady_used=[2])
    raw = json.dumps({"target_no": 4, "monologue": "m"}, ensure_ascii=False)
    pr = r.parse(2, raw, st)
    st2 = r.apply(st, pr.action)
    assert st2.lady_seen == "evil"  # 4 号是刺客
    # 查验选择本身只进观众流（公开宣布在下一阶段由持有者自己说）
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {"audience"}


# ---------------------------------------------------------------- view 信息隔离

def test_merlin_intel_visible_to_merlin_only() -> None:
    r = _rules()
    st = _state(phase="discuss")
    merlin = r.view(1, st, [], first_speak=True)
    assert sorted(merlin.intel) == [4, 5]  # 梅林看到刺客+莫甘娜（莫德雷德不在局）
    servant = r.view(3, st, [], first_speak=True)
    assert servant.intel == []
    morgana = r.view(5, st, [], first_speak=True)
    assert sorted(morgana.intel) == [4]  # 红方互认（不含自己）


def test_percival_sees_two_magicians_without_identity() -> None:
    r = _rules()
    st = _state(phase="discuss")
    percival = r.view(2, st, [], first_speak=True)
    assert sorted(percival.intel) == [1, 5]  # 梅林与莫甘娜，分不清谁是谁


def test_first_speak_seeds_context_later_speak_is_incremental() -> None:
    r = _rules()
    st = _state(phase="discuss")
    first = r.view(1, st, [], first_speak=True)
    again = r.view(1, st, [], first_speak=False)
    assert "固定设定" in first.prompt
    assert "固定设定" not in again.prompt  # 续跑/再次发言只带增量，session 里已有上下文
