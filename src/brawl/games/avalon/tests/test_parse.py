"""AvalonRules.parse 的硬校验与信息隔离。

非法产出（坏队伍、蓝方出破坏票、刺错阵营、查验非法对象）一律 JsonParseError，交给
loop 重试，绝不静默上桌。隔离不变量：任务出牌/组队表决的产出只进观众流；梅林看到的
红方名单绝不进其他席位的 view。
"""

from __future__ import annotations

import json

import pytest

from brawl.games.avalon import prompts
from brawl.games.avalon import rules as av_rules
from brawl.games.avalon.rules_engine import AvalonRules, AvalonState
from brawl.games.avalon.state import Role, Seat
from brawl.games.avalon.view import PlayerView
from brawl.runtime.parsing import JsonParseError
from brawl.runtime.threads import SeatThread, ThreadMessage

# 5 人局：1 梅林，2 派西维尔，3 忠臣，4 刺客，5 莫甘娜（蓝 3 红 2）
_ROLES = [Role.MERLIN, Role.PERCIVAL, Role.SERVANT, Role.ASSASSIN, Role.MORGANA]


def _rules() -> AvalonRules:
    return AvalonRules()


def _state(**over) -> AvalonState:
    seats = [
        Seat(
            no=i + 1,
            persona_id="fox",
            strategy_id="prudent",
            tone="tone",
            strategy="strategy",
            role=role,
            intel=av_rules.night_intel(_ROLES, i + 1),
        )
        for i, role in enumerate(_ROLES)
    ]
    st = AvalonState(seats=seats, leader=1)
    return st.model_copy(deep=True, update=over)


def _proposal(team: list[int]) -> str:
    return json.dumps(
        {"phase": "propose", "team": team, "speech": "听我的", "monologue": "m"},
        ensure_ascii=False,
    )


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
    team_record = next(d for d in pr.deliveries if "提议的队伍" in d.text)
    assert team_record.spoken is False


@pytest.mark.parametrize(
    "payload",
    [
        {"team": [1, 3], "speech": "听我的", "monologue": "m"},
        {"phase": "quest", "team": [1, 3], "speech": "听我的", "monologue": "m"},
        {"phase": "propose", "team": [True, 3], "speech": "听我的", "monologue": "m"},
        {"phase": "propose", "team": [1, 3], "speech": "听我的", "monologue": "m", "extra": 1},
    ],
)
def test_propose_schema_rejects_missing_wrong_or_coerced_fields(payload: dict) -> None:
    with pytest.raises(JsonParseError):
        _rules().parse(1, json.dumps(payload, ensure_ascii=False), _state(phase="propose"))


def test_shared_statement_phase_must_match_runtime_phase() -> None:
    raw = json.dumps(
        {"phase": "banter", "speech": "这不是讨论阶段", "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="应为 discuss"):
        _rules().parse(2, raw, _state(phase="discuss", team=[1, 3], discuss_index=0))


# ---------------------------------------------------------------- 任务出牌

def test_good_side_cannot_play_fail() -> None:
    st = _state(phase="quest", team=[1, 4])
    raw = json.dumps({"phase": "quest", "success": False, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError, match="蓝方"):
        _rules().parse(1, raw, st)


def test_evil_side_may_play_fail() -> None:
    st = _state(phase="quest", team=[1, 4])
    raw = json.dumps({"phase": "quest", "success": False, "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(4, raw, st)
    assert json.loads(pr.action)["success"] is False


def test_quest_card_never_reaches_seats() -> None:
    """谁出了什么牌永远保密：任务出牌产出只进观众流。"""
    st = _state(phase="quest", team=[1, 4])
    raw = json.dumps({"phase": "quest", "success": True, "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(1, raw, st)
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {"audience"}


def test_team_vote_never_reaches_seats_before_tally() -> None:
    """亮票前票型保密：表决产出只进观众流（票型由 host 开票句公布）。"""
    st = _state(phase="team_vote", team=[1, 4])
    raw = json.dumps({"phase": "team_vote", "approve": True, "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(2, raw, st)
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {"audience"}


def test_team_vote_normalizes_string_boolean() -> None:
    """模型把布尔写成 "false" 字符串时按 schema 声明无损归一化（GLM 系怪癖）。"""
    raw = json.dumps({"phase": "team_vote", "approve": "false", "monologue": "m"})
    pr = _rules().parse(2, raw, _state(phase="team_vote", team=[1, 4]))
    assert json.loads(pr.action)["approve"] is False


def test_team_vote_rejects_non_boolean_string() -> None:
    raw = json.dumps({"phase": "team_vote", "approve": "yes", "monologue": "m"})
    with pytest.raises(JsonParseError):
        _rules().parse(2, raw, _state(phase="team_vote", team=[1, 4]))


# ---------------------------------------------------------------- 刺杀


def test_final_evil_chat_reaches_all_evil_and_audience_but_no_good_seat() -> None:
    r = _rules()
    st = _state(
        phase="evil_chat",
        evil_chat_index=0,
        evil_team=[4, 5],
    )
    raw = json.dumps(
        {"phase": "evil_chat", "speech": "一号最像梅林", "monologue": "交刀口"},
        ensure_ascii=False,
    )
    parsed = r.parse(4, raw, st)
    speech = parsed.deliveries[0]
    assert set(speech.channels) == {4, 5, "audience"}
    assert "broadcast" not in speech.channels

    view = r.view(4, st, SeatThread(), first_speak=True)
    assert view.evil_team == [4, 5]
    assert view.intel == [5]
    assert "完整红方名单" in view.prompt
    assert "终局互认已经取代开局互认限制" in view.turn_prompt


def test_assassin_cannot_target_known_teammate() -> None:
    st = _state(phase="assassinate", evil_team=[4, 5])
    raw = json.dumps(
        {"phase": "assassinate", "target_no": 5, "speech": "就是你了", "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="非法目标"):
        _rules().parse(4, raw, st)


def test_assassin_cannot_stab_oberon_after_final_evil_reveal() -> None:
    """奥伯伦开局不互认，但终局私聊明确把他加入完整红方名单。"""
    from brawl.games.avalon.state import Seat as _Seat

    roles = [Role.MERLIN, Role.SERVANT, Role.SERVANT, Role.ASSASSIN, Role.OBERON, Role.MORDRED, Role.MINION]
    seats = [
        _Seat(
            no=i + 1,
            persona_id="fox",
            strategy_id="prudent",
            role=r,
            intel=[],
        )
        for i, r in enumerate(roles)
    ]
    st = AvalonState(
        seats=seats,
        leader=1,
        phase="assassinate",
        evil_team=[4, 5, 6, 7],
    )
    raw = json.dumps(
        {"phase": "assassinate", "target_no": 5, "speech": "就是你了", "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="非法目标"):
        _rules().parse(4, raw, st)

    rules = _rules()
    view = rules.view(4, st, SeatThread(), first_speak=True)
    assert view.good_suspects() == [1, 2, 3]
    assert all(f"{seat}号" not in view.turn_prompt for seat in st.evil_team)
    random_target = json.loads(rules.random_action(4, view))["target_no"]
    assert random_target in view.good_suspects()


def test_assassin_hitting_merlin_flips_winner() -> None:
    r = _rules()
    st = _state(phase="assassinate", evil_team=[4, 5])
    raw = json.dumps(
        {"phase": "assassinate", "target_no": 1, "speech": "就是你了", "monologue": "m"},
        ensure_ascii=False,
    )
    pr = r.parse(4, raw, st)
    st2 = r.apply(st, pr.action)
    assert json.loads(pr.action)["hit"] is True
    assert st2.winner == "evil" and st2.win_reason == "assassination"


def test_assassin_missing_merlin_good_wins() -> None:
    r = _rules()
    st = _state(phase="assassinate", evil_team=[4, 5])
    raw = json.dumps(
        {"phase": "assassinate", "target_no": 3, "speech": "就是你了", "monologue": "m"},
        ensure_ascii=False,
    )
    pr = r.parse(4, raw, st)
    st2 = r.apply(st, pr.action)
    assert json.loads(pr.action)["hit"] is False
    assert st2.winner == "good"


# ---------------------------------------------------------------- 湖中仙女

def test_lady_cannot_recheck_previous_holder() -> None:
    st = _state(phase="lady_choose", lady_holder=2, lady_used=[2, 4])
    raw = json.dumps({"phase": "lady_choose", "target_no": 4, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(2, raw, st)


def test_lady_choice_reveals_side_to_holder_only() -> None:
    r = _rules()
    st = _state(phase="lady_choose", lady_holder=2, lady_used=[2])
    raw = json.dumps({"phase": "lady_choose", "target_no": 4, "monologue": "m"}, ensure_ascii=False)
    pr = r.parse(2, raw, st)
    st2 = r.apply(st, pr.action)
    assert st2.lady_seen == "evil"  # 4 号是刺客
    # 玩家动作只含 audience 内心戏；裁判紧接着公开查验目标，但绝不公开真阵营。
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {"audience"}
    [choice] = r.pending(st2)
    assert json.loads(choice.action) == {
        "kind": "lady_choice",
        "holder": 2,
        "target": 4,
    }
    assert choice.deliveries[0].channels == ["broadcast", "audience"]
    assert "二号选择查验四号" in choice.deliveries[0].text
    assert "红方" not in choice.deliveries[0].text


def test_lady_truth_is_projected_only_to_current_holder_view() -> None:
    r = _rules()
    st = _state(
        phase="lady_announce",
        lady_holder=2,
        lady_used=[2],
        lady_target=4,
        lady_seen="evil",
    )

    holder = r.view(2, st, SeatThread(), first_speak=True)
    assert holder.lady_seen == "evil"
    assert "红方" in holder.prompt
    with pytest.raises(ValueError, match="无权读取"):
        r.view(3, st, SeatThread(), first_speak=True)


# ---------------------------------------------------------------- view 信息隔离

def test_merlin_intel_visible_to_merlin_only() -> None:
    r = _rules()
    st = _state(phase="propose", leader=1)
    merlin = r.view(1, st, SeatThread(), first_speak=True)
    assert sorted(merlin.intel) == [4, 5]  # 梅林看到刺客+莫甘娜（莫德雷德不在局）
    servant = r.view(3, st.model_copy(update={"leader": 3}), SeatThread(), first_speak=True)
    assert servant.intel == []
    morgana = r.view(5, st.model_copy(update={"leader": 5}), SeatThread(), first_speak=True)
    assert sorted(morgana.intel) == [4]  # 红方互认（不含自己）


def test_percival_sees_two_magicians_without_identity() -> None:
    r = _rules()
    st = _state(phase="propose", leader=2)
    percival = r.view(2, st, SeatThread(), first_speak=True)
    assert sorted(percival.intel) == [1, 5]  # 梅林与莫甘娜，分不清谁是谁


def test_first_speak_seeds_context_later_speak_is_incremental() -> None:
    r = _rules()
    st = _state(phase="propose", leader=1)
    thread = SeatThread(
        inbox=["刚刚公开发生的事"],
        messages=[
            ThreadMessage(role="human", content="旧增量提问"),
            ThreadMessage(role="assistant", content="旧原始回答"),
        ],
    )
    first = r.view(1, st, thread, first_speak=True)
    again = r.view(1, st, thread, first_speak=False)
    assert "固定设定" in first.prompt
    assert "旧增量提问" in first.prompt and "旧原始回答" in first.prompt
    assert "固定设定" not in again.prompt  # 续跑/再次发言只带增量，session 里已有上下文
    assert first.turn_prompt == again.turn_prompt
    assert "刚刚公开发生的事" in first.turn_prompt


def test_prompt_only_requests_exact_json_without_freeform_escape_hatch() -> None:
    r = _rules()
    st = _state(phase="propose", leader=1)
    view = r.view(1, st, SeatThread(), first_speak=True)
    assert "自由思考" not in prompts.RULES
    assert "JSON 外" not in prompts.RULES
    assert "自检" not in view.prompt
    assert "StructuredOutput 工具" in view.prompt


def test_view_attaches_phase_schema_for_structured_output() -> None:
    r = _rules()
    st = _state(phase="propose", leader=1)
    view = r.view(1, st, SeatThread(), first_speak=True)
    schema = view.schema
    assert schema is not None
    assert schema["properties"]["phase"] == {"const": "propose"}
    assert set(schema["required"]) == {"phase", "team", "speech", "monologue"}


def test_oberon_final_prompt_uses_revealed_team_not_opening_isolation() -> None:
    view = PlayerView(
        seat_no=5,
        role=Role.OBERON,
        intel=[4, 6],
        lineup=[
            Role.MERLIN,
            Role.SERVANT,
            Role.SERVANT,
            Role.ASSASSIN,
            Role.OBERON,
            Role.MINION,
        ],
        players=6,
        tone="tone",
        strategy="strategy",
        phase="evil_chat",
        evil_team=[4, 5, 6],
    )
    # 固定 system 仍忠实描述开局夜视；当轮增量明确宣布规则转换，保证 live / 冷恢复一致。
    assert "你不认识同伙" in prompts.system_text(view)
    turn = prompts.opencode_ask("evil_chat", view)
    assert "完整红方名单是：4号、5号、6号" in turn
    assert "终局互认已经取代开局互认限制" in turn
