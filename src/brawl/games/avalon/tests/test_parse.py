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
from brawl.games.avalon.state import ProposalRecord, Role, Seat
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


def test_v6_recognized_evil_players_use_deterministic_sabotage_order() -> None:
    st = _state(
        phase="quest",
        team=[4, 5],
        agent_protocol=6,
    )
    sabotage = json.dumps(
        {"tool": {"name": "submit_quest", "input": {"success": False}}},
        ensure_ascii=False,
    )
    success = json.dumps(
        {"tool": {"name": "submit_quest", "input": {"success": True}}},
        ensure_ascii=False,
    )

    assert json.loads(_rules().parse(4, sabotage, st).action)["success"] is False
    with pytest.raises(JsonParseError, match="避免互认红方重复破坏"):
        _rules().parse(5, sabotage, st)
    assert json.loads(_rules().parse(5, success, st).action)["success"] is True


def test_v5_replay_keeps_pre_coordination_sabotage_semantics() -> None:
    st = _state(
        phase="quest",
        team=[4, 5],
        agent_protocol=5,
    )
    sabotage = json.dumps(
        {"tool": {"name": "submit_quest", "input": {"success": False}}},
        ensure_ascii=False,
    )

    assert json.loads(_rules().parse(5, sabotage, st).action)["success"] is False


def test_v8_does_not_turn_evil_strategy_into_action_legality() -> None:
    st = _state(
        phase="quest",
        team=[4, 5],
        agent_protocol=8,
    )
    sabotage = json.dumps(
        {"tool": {"name": "submit_quest", "input": {"success": False}}},
        ensure_ascii=False,
    )

    assert json.loads(_rules().parse(4, sabotage, st).action)["success"] is False
    assert json.loads(_rules().parse(5, sabotage, st).action)["success"] is False


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


# ---------------------------------------------------------------- role-agent/tool protocol v2

def test_v2_proposal_keeps_natural_speech_out_of_tool_and_has_no_monologue() -> None:
    st = _state(phase="propose", agent_protocol=2)
    raw = json.dumps(
        {
            "text": "一号三号先走，至少这轮逻辑我能负责。",
            "tool": {"name": "submit_team", "input": {"team": [1, 3]}},
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(1, raw, st)
    action = json.loads(parsed.action)

    assert action["team"] == [1, 3]
    assert action["speech"] == "一号三号先走，至少这轮逻辑我能负责。"
    assert action["monologue"] == ""
    assert all("audience" not in delivery.channels for delivery in parsed.deliveries)


def test_v2_action_only_turn_uses_tool_input_without_audience_delivery() -> None:
    st = _state(phase="team_vote", team=[1, 4], agent_protocol=2)
    raw = json.dumps(
        {"tool": {"name": "submit_vote", "input": {"approve": False}}},
        ensure_ascii=False,
    )

    parsed = _rules().parse(2, raw, st)

    assert json.loads(parsed.action)["approve"] is False
    assert parsed.deliveries == []


def test_v2_rejects_wrong_tool_and_missing_speech() -> None:
    with pytest.raises(JsonParseError, match="应提交 submit_team"):
        _rules().parse(
            1,
            json.dumps(
                {"text": "发车", "tool": {"name": "submit_vote", "input": {"approve": True}}},
                ensure_ascii=False,
            ),
            _state(phase="propose", agent_protocol=2),
        )
    with pytest.raises(JsonParseError, match="缺少公开发言"):
        _rules().parse(
            1,
            json.dumps(
                {"tool": {"name": "submit_team", "input": {"team": [1, 3]}}},
                ensure_ascii=False,
            ),
            _state(phase="propose", agent_protocol=2),
        )


def test_v7_proposal_requires_speech_to_name_selected_members() -> None:
    st = _state(phase="propose", agent_protocol=7)
    generic = json.dumps(
        {
            "text": "我先给出一个稳妥方案，大家可以据此判断。",
            "tool": {"name": "submit_team", "input": {"team": [1, 3]}},
        },
        ensure_ascii=False,
    )

    with pytest.raises(JsonParseError, match="逐一点出"):
        _rules().parse(1, generic, st)

    grounded = json.dumps(
        {
            "text": "我带1号和3号：1号作为队长承担结果，3号当前没有矛盾票型。",
            "tool": {"name": "submit_team", "input": {"team": [1, 3]}},
        },
        ensure_ascii=False,
    )
    assert json.loads(_rules().parse(1, grounded, st).action)["team"] == [1, 3]


def test_v2_proposal_keeps_replay_compatible_generic_speech() -> None:
    st = _state(phase="propose", agent_protocol=2)
    raw = json.dumps(
        {
            "text": "我先给出一个稳妥方案，大家可以据此判断。",
            "tool": {"name": "submit_team", "input": {"team": [1, 3]}},
        },
        ensure_ascii=False,
    )

    assert json.loads(_rules().parse(1, raw, st).action)["team"] == [1, 3]


def test_v6_replay_is_not_revalidated_by_v7_quality_gates() -> None:
    st = _state(phase="propose", agent_protocol=6)
    raw = json.dumps(
        {
            "text": "我先给出一个稳妥方案。",
            "tool": {"name": "submit_team", "input": {"team": [1, 3]}},
        },
        ensure_ascii=False,
    )

    assert json.loads(_rules().parse(1, raw, st).action)["team"] == [1, 3]


def test_v7_good_discussion_cannot_treat_success_as_identity_proof() -> None:
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=7,
    )
    overclaim = json.dumps(
        {
            "text": "上一轮任务成功且零张破坏，已经证明1号和3号全蓝，所以这轮应继续信任。",
        },
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="不能推出全蓝"):
        _rules().parse(2, overclaim, st)

    calibrated = json.dumps(
        {
            "text": "上一轮任务成功但不代表1号和3号都清白，红方也可能出成功；"
            "我只把它当弱概率证据。",
        },
        ensure_ascii=False,
    )
    assert _rules().parse(2, calibrated, st).deliveries

    probabilistic = json.dumps(
        {
            "text": "上一轮任务成功，结合1号连续反对两支失败车，我认为他相对更可信。",
        },
        ensure_ascii=False,
    )
    assert _rules().parse(2, probabilistic, st).deliveries

    contradictory = json.dumps(
        {
            "text": "红方也会出成功，但这次零张破坏已经证明1号和3号全蓝。",
        },
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="不能推出全蓝"):
        _rules().parse(2, contradictory, st)


def test_v7_good_discussion_cannot_use_identity_claim_as_support() -> None:
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=7,
    )
    credulous = json.dumps(
        {
            "text": "3号已经表态自己是蓝方，我没有理由质疑他，所以会赞成这支队伍。",
        },
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="零成本自称"):
        _rules().parse(2, credulous, st)

    discounted = json.dumps(
        {
            "text": "3号自称蓝方不能作为可信证据；他上一轮反对自己提出的核心成员，"
            "这个票型矛盾才是我反对的依据。",
        },
        ensure_ascii=False,
    )
    assert _rules().parse(2, discounted, st).deliveries

    separate_reason = json.dumps(
        {
            "text": "3号自称蓝方；我赞成是因为他此前两轮票型都与失败车切割。",
        },
        ensure_ascii=False,
    )
    assert _rules().parse(2, separate_reason, st).deliveries

    cross_subject = json.dumps(
        {
            "text": "4号声称查到我为红方，我明确否认——我是蓝方；"
            "但我赞成这车是因为1号的票型连续。",
        },
        ensure_ascii=False,
    )
    assert _rules().parse(2, cross_subject, st).deliveries


def test_v7_good_player_cannot_directly_trust_another_lady_announcement() -> None:
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=7,
        lady_checks={1: {3: "good"}},
    )
    credulous = json.dumps(
        {
            "text": "1号仙女宣布3号是蓝方，我相信这个结果，所以赞成3号上车。",
        },
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="仙女公布"):
        _rules().parse(2, credulous, st)

    skeptical = json.dumps(
        {
            "text": "1号仙女宣布3号是蓝方，但这只是可撒谎的声明，仍需看3号票型。",
        },
        ensure_ascii=False,
    )
    assert _rules().parse(2, skeptical, st).deliveries

    # 查验者本人掌握真实私信，被查的蓝方也独立知道自己的阵营。
    assert _rules().parse(1, credulous, st).deliveries
    assert _rules().parse(3, credulous, st).deliveries


def test_v7_strong_public_stance_binds_secret_vote() -> None:
    rules = _rules()
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=7,
    )
    discussed = rules.parse(
        2,
        json.dumps(
            {"text": "我明确反对这支队伍，我会投反对票。"},
            ensure_ascii=False,
        ),
        st,
    )
    after = rules.apply(st, discussed.action).model_copy(
        update={"phase": "team_vote"}
    )

    opposite = json.dumps(
        {"tool": {"name": "submit_vote", "input": {"approve": True}}},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="公开明确承诺投反对"):
        rules.parse(2, opposite, after)

    matching = json.dumps(
        {"tool": {"name": "submit_vote", "input": {"approve": False}}},
        ensure_ascii=False,
    )
    assert json.loads(rules.parse(2, matching, after).action)["approve"] is False


@pytest.mark.parametrize(
    ("speech", "expected"),
    [
        ("赞成。", True),
        ("2号赞成。", True),
        ("2号：反对。这个队伍没有可核对依据。", False),
        ("我同意7号的顾虑，但目前倾向反对。", None),
    ],
)
def test_v7_bare_leading_stance_is_recorded(
    speech: str,
    expected: bool | None,
) -> None:
    parsed = _rules().parse(
        2,
        json.dumps({"text": speech}, ensure_ascii=False),
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            agent_protocol=7,
        ),
    )

    assert json.loads(parsed.action)["declared_stance"] is expected


def test_v7_tentative_stance_can_change_after_later_discussion() -> None:
    rules = _rules()
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=7,
    )
    discussed = rules.parse(
        2,
        json.dumps(
            {"text": "我目前倾向反对，但会继续听后置位的具体证据。"},
            ensure_ascii=False,
        ),
        st,
    )
    after = rules.apply(st, discussed.action).model_copy(
        update={"phase": "team_vote"}
    )
    approve = json.dumps(
        {"tool": {"name": "submit_vote", "input": {"approve": True}}},
        ensure_ascii=False,
    )

    assert json.loads(rules.parse(2, approve, after).action)["approve"] is True


def test_v7_lady_choice_records_private_source_for_later_epistemic_check() -> None:
    rules = _rules()
    st = _state(
        phase="lady_choose",
        lady_holder=2,
        lady_used=[2],
        agent_protocol=7,
    )
    raw = json.dumps(
        {"tool": {"name": "submit_lady", "input": {"target_no": 3}}},
        ensure_ascii=False,
    )
    chosen = rules.parse(2, raw, st)
    after = rules.apply(st, chosen.action)

    assert after.lady_checks == {2: {3: "good"}}


def test_v7_evil_may_intentionally_overstate_success_as_identity_proof() -> None:
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=7,
    )
    raw = json.dumps(
        {
            "text": "上一轮任务成功且零张破坏，已经证明1号和3号全蓝，所以这轮应继续信任。",
        },
        ensure_ascii=False,
    )

    assert _rules().parse(4, raw, st).deliveries


def test_v7_discussion_rejects_near_duplicate_but_accepts_new_claim() -> None:
    previous = (
        "我反对这支队伍，因为上一轮任务失败后仍然保留了核心成员，"
        "在没有新增验证的情况下风险没有下降，我会投反对票。"
    )
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=1,
        discussion_speeches={1: previous},
        agent_protocol=7,
    )
    duplicate = json.dumps(
        {
            "text": (
                "我也反对这支队伍，因为上一轮任务失败后还是保留了核心成员，"
                "没有新增验证时风险并没有下降，所以我会投反对票。"
            ),
        },
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="高度重复"):
        _rules().parse(2, duplicate, st)

    new_claim = json.dumps(
        {
            "text": "我反对的新增理由是3号上一轮先要求整队更换，本轮却接受原队核心，"
            "这个立场反转至今没有解释。",
        },
        ensure_ascii=False,
    )
    assert _rules().parse(2, new_claim, st).deliveries


def test_v7_discussion_rejects_short_template_but_allows_explicit_pass() -> None:
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=1,
        discussion_speeches={1: "同意，先走一轮看看。"},
        agent_protocol=7,
    )
    repeated = json.dumps(
        {"text": "我也同意先走一轮，看看任务结果再说。"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="首轮缺少证据"):
        _rules().parse(2, repeated, st)

    passed = json.dumps({"text": "过。"}, ensure_ascii=False)
    assert _rules().parse(2, passed, st).deliveries[0].text == "过。"


@pytest.mark.parametrize(
    ("previous", "repeated", "label"),
    [
        (
            "第一轮信息太少，这支队伍缺乏信任依据，我倾向反对。",
            "首轮信息不足，队伍里没有我信任的人，我目前倾向反对。",
            "首轮缺少证据",
        ),
        (
            "我在这支队伍里，会出成功牌，所以支持通过。",
            "我是这支队伍的成员，自然赞成自己的队伍。",
            "在队所以支持自己的队伍",
        ),
        (
            "这三人都是前排连续号，队伍缺少多样性和测试意义。",
            "没有后排座位参与，这种连续号阵容无法提供有效检验。",
            "仅按座位前后",
        ),
    ],
)
def test_v7_discussion_collapses_common_low_information_reasons_to_pass(
    previous: str,
    repeated: str,
    label: str,
) -> None:
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=1,
        discussion_speeches={1: previous},
        agent_protocol=7,
    )
    raw = json.dumps({"text": repeated}, ensure_ascii=False)

    with pytest.raises(JsonParseError, match=label):
        _rules().parse(2, raw, st)


def test_v7_discussion_rejects_exact_short_duplicate() -> None:
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=1,
        discussion_speeches={1: "这车风险太高，我反对。"},
        agent_protocol=7,
    )
    duplicate = json.dumps(
        {"text": "这车风险太高，我反对。"},
        ensure_ascii=False,
    )

    with pytest.raises(JsonParseError, match="高度重复"):
        _rules().parse(2, duplicate, st)


def test_v7_discussion_history_is_recorded_and_reset_by_new_proposal() -> None:
    rules = _rules()
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=7,
    )
    discussed = rules.apply(
        st,
        json.dumps(
            {
                "kind": "discuss",
                "seat": 2,
                "speech": "我关注3号此前的票型矛盾，因此这轮反对。",
                "monologue": "",
            },
            ensure_ascii=False,
        ),
    )
    assert discussed.discussion_speeches == {
        2: "我关注3号此前的票型矛盾，因此这轮反对。"
    }

    proposed = rules.apply(
        discussed,
        json.dumps(
            {
                "kind": "propose",
                "seat": 1,
                "team": [1, 3],
                "speech": "1号和3号上任务。",
                "monologue": "",
            },
            ensure_ascii=False,
        ),
    )
    assert proposed.discussion_speeches == {1: "1号和3号上任务。"}
    assert proposed.discussion_stances == {1: True}


def test_v8_accepts_natural_language_without_semantic_adjudication() -> None:
    rules = _rules()
    proposal_state = _state(phase="propose", agent_protocol=8)
    generic_proposal = json.dumps(
        {
            "text": "我先给一个方案，理由稍后结合大家的反应再展开。",
            "tool": {"name": "submit_team", "input": {"team": [1, 3]}},
        },
        ensure_ascii=False,
    )
    assert json.loads(rules.parse(1, generic_proposal, proposal_state).action)[
        "team"
    ] == [1, 3]

    discussion_state = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=1,
        discussion_speeches={1: "这车风险太高，我反对。"},
        lady_checks={1: {3: "good"}},
        agent_protocol=8,
    )
    speeches = (
        "这车风险太高，我反对。",
        "上一轮零破坏，所以我暂时把1号和3号都当成蓝方。",
        "1号仙女宣布3号是蓝方，我相信这个结果，所以赞成。",
        "3号说自己是蓝方，我愿意相信他。",
    )
    for speech in speeches:
        parsed = rules.parse(
            2,
            json.dumps({"text": speech}, ensure_ascii=False),
            discussion_state,
        )
        assert parsed.deliveries[0].text == speech
        assert "declared_stance" not in json.loads(parsed.action)


def test_v8_vote_is_not_rejected_by_guessing_meaning_of_prior_speech() -> None:
    st = _state(
        phase="team_vote",
        team=[1, 3],
        discussion_stances={2: False},
        agent_protocol=8,
    )
    approve = json.dumps(
        {"tool": {"name": "submit_vote", "input": {"approve": True}}},
        ensure_ascii=False,
    )

    assert json.loads(_rules().parse(2, approve, st).action)["approve"] is True


def test_v8_does_not_duplicate_transcript_for_language_gates() -> None:
    rules = _rules()
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=8,
    )
    parsed = rules.parse(
        2,
        json.dumps({"text": "我反对。"}, ensure_ascii=False),
        st,
    )
    after = rules.apply(st, parsed.action)

    assert after.discussion_speeches == {}
    assert after.discussion_stances == {}


def test_v10_player_declares_public_position_without_reading_speech() -> None:
    rules = _rules()
    st = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=10,
    )
    raw = json.dumps(
        {
            "text": "我目前支持这支队伍。",
            "tool": {
                "name": "submit_statement",
                "input": {"position": "approve"},
            },
        },
        ensure_ascii=False,
    )

    parsed = rules.parse(2, raw, st)
    action = json.loads(parsed.action)
    after = rules.apply(st, parsed.action)

    assert action["position"] == "approve"
    assert after.discussion_positions == {2: "approve"}
    assert after.discussion_speeches == {}
    assert parsed.deliveries[1].text == "2号 当前票意：赞成。"
    assert parsed.deliveries[1].spoken is False


def test_v10_structured_position_is_public_even_if_text_conflicts() -> None:
    raw = json.dumps(
        {
            "text": "我反对这支队伍。",
            "tool": {
                "name": "submit_statement",
                "input": {"position": "approve"},
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(
        2,
        raw,
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            agent_protocol=10,
        ),
    )

    assert parsed.deliveries[0].text == "我反对这支队伍。"
    assert parsed.deliveries[1].text == "2号 当前票意：赞成。"
    assert json.loads(parsed.action)["position"] == "approve"


def test_v9_statement_rejects_only_invalid_structured_position() -> None:
    raw = json.dumps(
        {
            "text": "这句话可以自由表达。",
            "tool": {
                "name": "submit_statement",
                "input": {"position": "maybe"},
            },
        },
        ensure_ascii=False,
    )

    with pytest.raises(JsonParseError):
        _rules().parse(
            2,
            raw,
            _state(
                phase="discuss",
                team=[1, 3],
                discuss_index=0,
                agent_protocol=9,
            ),
        )


def test_v10_changed_vote_is_explained_instead_of_rejected() -> None:
    st = _state(
        phase="team_vote",
        team=[1, 3],
        discussion_positions={2: "approve"},
        agent_protocol=10,
    )
    changed = json.dumps(
        {
            "tool": {
                "name": "submit_ballot",
                "input": {
                    "approve": False,
                    "reason": "后置位指出3号上一轮反投自己的提案，这改变了我的判断。",
                },
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(2, changed, st)
    action = json.loads(parsed.action)

    assert action["approve"] is False
    assert action["changed_from"] == "approve"
    assert action["position"] == "reject"
    assert action["speech"] == parsed.deliveries[0].text
    assert len(parsed.deliveries) == 1
    assert parsed.deliveries[0].channels == ["broadcast"]
    assert "从赞成改为反对" in parsed.deliveries[0].text
    assert "后置位指出3号" in parsed.deliveries[0].text


def test_v10_unchanged_vote_keeps_reason_private() -> None:
    st = _state(
        phase="team_vote",
        team=[1, 3],
        discussion_positions={2: "reject"},
        agent_protocol=10,
    )
    unchanged = json.dumps(
        {
            "tool": {
                "name": "submit_ballot",
                "input": {
                    "approve": False,
                    "reason": "这支队伍仍然保留了上一轮失败车的核心成员。",
                },
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(2, unchanged, st)

    assert json.loads(parsed.action)["approve"] is False
    assert parsed.deliveries == []


@pytest.mark.parametrize(
    ("phase", "payload", "tool"),
    [
        (
            "discuss",
            {"phase": "discuss", "speech": "旧格式", "monologue": "m"},
            "submit_statement",
        ),
        (
            "team_vote",
            {"phase": "team_vote", "approve": True, "monologue": "m"},
            "submit_ballot",
        ),
    ],
)
def test_v9_rejects_legacy_phase_json_with_clear_tool_error(
    phase: str,
    payload: dict,
    tool: str,
) -> None:
    with pytest.raises(JsonParseError, match=tool):
        _rules().parse(
            2,
            json.dumps(payload, ensure_ascii=False),
            _state(
                phase=phase,
                team=[1, 3],
                discuss_index=0,
                agent_protocol=9,
            ),
        )


def test_v10_proposal_records_leader_initial_public_position() -> None:
    rules = _rules()
    st = _state(phase="propose", agent_protocol=10)
    raw = json.dumps(
        {
            "text": "我提议1号和3号执行任务。",
            "tool": {"name": "submit_team", "input": {"team": [1, 3]}},
        },
        ensure_ascii=False,
    )

    proposed = rules.parse(1, raw, st)
    after = rules.apply(st, proposed.action)

    action = json.loads(proposed.action)
    assert action["position"] == "approve"
    assert after.discussion_positions == {1: "approve"}
    assert proposed.deliveries[2].text == "1号 当前票意：赞成。"
    assert proposed.deliveries[2].spoken is False


def test_v10_view_selects_position_and_ballot_tools() -> None:
    rules = _rules()
    discuss = rules.view(
        2,
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            agent_protocol=10,
        ),
        SeatThread(),
    )
    vote = rules.view(
        2,
        _state(
            phase="team_vote",
            team=[1, 3],
            discussion_positions={2: "reject"},
            agent_protocol=10,
        ),
        SeatThread(),
    )

    assert discuss.action_tool == "submit_statement"
    assert discuss.speech_required is True
    assert "position=approve" in discuss.prompt
    assert vote.action_tool == "submit_ballot"
    assert vote.speech_required is False
    assert vote.public_position == "reject"
    assert "公开票意是“反对”" in vote.prompt


def test_v15_proposal_uses_only_tool_speech() -> None:
    rules = _rules()
    state = _state(phase="propose", agent_protocol=15)
    raw = json.dumps(
        {
            "text": "私有分析：我是红方，三号是我的同伙。",
            "tool": {
                "name": "submit_team",
                "input": {
                    "team": [1, 3],
                    "speech": "我提议一号和三号，先看首轮任务结果。",
                },
            },
        },
        ensure_ascii=False,
    )

    parsed = rules.parse(1, raw, state)
    action = json.loads(parsed.action)

    assert action["speech"] == "我提议一号和三号，先看首轮任务结果。"
    assert all("我是红方" not in delivery.text for delivery in parsed.deliveries)
    assert parsed.deliveries[0].text == action["speech"]


def test_v15_discussion_uses_tool_speech_and_position() -> None:
    state = _state(
        phase="discuss",
        team=[1, 3],
        discuss_index=0,
        agent_protocol=15,
    )
    raw = json.dumps(
        {
            "text": "这里是工具外推理，不应上桌。",
            "tool": {
                "name": "submit_statement",
                "input": {
                    "position": "reject",
                    "speech": "三号缺少公开履历，我目前反对这支队伍。",
                },
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(2, raw, state)
    action = json.loads(parsed.action)

    assert action["speech"] == "三号缺少公开履历，我目前反对这支队伍。"
    assert action["position"] == "reject"
    assert parsed.deliveries[0].text == action["speech"]
    assert all("工具外推理" not in delivery.text for delivery in parsed.deliveries)


@pytest.mark.parametrize(
    ("phase", "state_updates", "seat", "channels"),
    [
        (
            "lady_announce",
            {"lady_holder": 2, "lady_target": 3, "lady_seen": "evil"},
            2,
            ["broadcast"],
        ),
        (
            "evil_chat",
            {"evil_chat_index": 0, "evil_team": [4, 5]},
            4,
            [4, 5, "audience"],
        ),
        (
            "banter",
            {"banter_index": 0, "winner": "evil"},
            1,
            ["broadcast"],
        ),
    ],
)
def test_v15_pure_speech_phases_use_submit_speech(
    phase: str,
    state_updates: dict,
    seat: int,
    channels: list,
) -> None:
    state = _state(
        phase=phase,
        agent_protocol=15,
        **state_updates,
    )
    raw = json.dumps(
        {
            "text": "工具外草稿。",
            "tool": {
                "name": "submit_speech",
                "input": {"speech": "这是实际发送给合法听众的话。"},
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(seat, raw, state)
    action = json.loads(parsed.action)

    assert action["speech"] == "这是实际发送给合法听众的话。"
    assert parsed.deliveries[0].text == action["speech"]
    assert parsed.deliveries[0].channels == channels


def test_v15_assassination_uses_tool_target_and_speech() -> None:
    state = _state(
        phase="assassinate",
        evil_team=[4, 5],
        agent_protocol=15,
    )
    raw = json.dumps(
        {
            "text": "私下真正的候选排序。",
            "tool": {
                "name": "submit_assassination",
                "input": {
                    "target_no": 1,
                    "speech": "综合整局轨迹，我选择刺杀一号。",
                },
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(4, raw, state)
    action = json.loads(parsed.action)

    assert action["target_no"] == 1
    assert action["speech"] == "综合整局轨迹，我选择刺杀一号。"
    assert parsed.deliveries[0].text == action["speech"]


@pytest.mark.parametrize(
    "tool_input",
    [
        {"team": [1, 3]},
        {"team": [1, 3], "speech": ""},
        {"team": [1, 3], "speech": "   "},
        {"team": [1, 3], "speech": "公开发言", "extra": True},
    ],
)
def test_v15_rejects_invalid_tool_speech_shape(tool_input: dict) -> None:
    raw = json.dumps(
        {
            "text": "不能作为回退来源。",
            "tool": {"name": "submit_team", "input": tool_input},
        },
        ensure_ascii=False,
    )

    with pytest.raises(JsonParseError):
        _rules().parse(1, raw, _state(phase="propose", agent_protocol=15))


def test_v15_rejects_legacy_phase_json_but_v14_keeps_old_envelope() -> None:
    legacy_phase = json.dumps(
        {
            "phase": "propose",
            "team": [1, 3],
            "speech": "旧 phase JSON",
            "monologue": "",
        },
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="submit_team"):
        _rules().parse(
            1,
            legacy_phase,
            _state(phase="propose", agent_protocol=15),
        )

    old_envelope = json.dumps(
        {
            "text": "旧协议仍以 assistant 文本为公开发言。",
            "tool": {"name": "submit_team", "input": {"team": [1, 3]}},
        },
        ensure_ascii=False,
    )
    parsed = _rules().parse(
        1,
        old_envelope,
        _state(phase="propose", agent_protocol=14),
    )
    assert json.loads(parsed.action)["speech"] == "旧协议仍以 assistant 文本为公开发言。"


def test_v15_view_selects_speech_tools_without_requiring_assistant_text() -> None:
    rules = _rules()
    discuss = rules.view(
        2,
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            agent_protocol=15,
        ),
        SeatThread(),
    )
    lady = rules.view(
        2,
        _state(
            phase="lady_announce",
            lady_holder=2,
            lady_target=3,
            lady_seen="evil",
            agent_protocol=15,
        ),
        SeatThread(),
    )

    assert discuss.action_tool == "submit_statement"
    assert discuss.speech_required is False
    assert "只有 speech 参数会公布上桌" in discuss.prompt
    assert lady.action_tool == "submit_speech"
    assert lady.speech_required is False


def test_v16_prompt_uses_private_deliberation_channel_without_forcing_filler() -> None:
    view = _rules().view(
        2,
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            agent_protocol=16,
        ),
        SeatThread(),
    )

    assert "行动前先在工具外私下核对" in view.prompt
    assert "不要用前面玩家重复表达的共识代替这次核对" in view.prompt
    assert "工具外文本不会成为玩家发言" in view.prompt
    assert "没有不同于前面发言的新依据，不必另造理由" in view.prompt
    assert "speech 可以只写“过”或一句立场" in view.prompt


def test_v17_private_basis_is_required_but_never_projected() -> None:
    state = _state(phase="propose", agent_protocol=17)
    raw = json.dumps(
        {
            "tool": {
                "name": "submit_team",
                "input": {
                    "team": [1, 3],
                    "speech": "我提议一号和三号执行任务。",
                    "private_basis": "一号是我自己；三号目前没有公开任务履历。",
                },
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(1, raw, state)
    action = json.loads(parsed.action)

    assert "private_basis" not in action
    assert all(
        "没有公开任务履历" not in delivery.text
        for delivery in parsed.deliveries
    )
    assert action["speech"] == "我提议一号和三号执行任务。"

    without_basis = json.dumps(
        {
            "tool": {
                "name": "submit_team",
                "input": {
                    "team": [1, 3],
                    "speech": "我提议一号和三号执行任务。",
                },
            },
        },
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="private_basis"):
        _rules().parse(1, without_basis, state)

    old = _rules().parse(
        1,
        without_basis,
        _state(phase="propose", agent_protocol=16),
    )
    assert json.loads(old.action)["team"] == [1, 3]


def test_v17_quest_basis_is_private_and_card_remains_the_models_decision() -> None:
    state = _state(
        phase="quest",
        team=[4, 5],
        votes={1: True, 2: True, 3: True, 4: True, 5: True},
        agent_protocol=17,
    )
    raw = json.dumps(
        {
            "tool": {
                "name": "submit_quest",
                "input": {
                    "success": False,
                    "private_basis": (
                        "我是本队座位最小的互认红方，按共同默认约定负责破坏。"
                    ),
                },
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(4, raw, state)
    action = json.loads(parsed.action)

    assert action["success"] is False
    assert "private_basis" not in action
    assert parsed.deliveries == []


def test_v17_view_requests_private_basis_for_strategic_tools() -> None:
    discuss = _rules().view(
        2,
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            agent_protocol=17,
        ),
        SeatThread(),
    )
    quest = _rules().view(
        4,
        _state(
            phase="quest",
            team=[4, 5],
            votes={1: True, 2: True, 3: True, 4: True, 5: True},
            agent_protocol=17,
        ),
        SeatThread(),
    )

    assert "private_basis 中私下写出直接影响票意" in discuss.prompt
    assert "private_basis 中私下核对阵营" in quest.prompt
    assert "行动前先在工具外私下核对" not in discuss.prompt


def test_v18_match_point_prompt_treats_mission_seats_as_win_resources() -> None:
    failed_records = [
        ProposalRecord(
            quest=1,
            attempt=1,
            leader_no=1,
            team=[1, 4],
            votes={1: True, 2: True, 3: True, 4: True, 5: True},
            approved=True,
            result="fail",
            fails=1,
        ),
        ProposalRecord(
            quest=2,
            attempt=1,
            leader_no=2,
            team=[2, 4, 5],
            votes={1: True, 2: True, 3: True, 4: True, 5: True},
            approved=True,
            result="fail",
            fails=1,
        ),
    ]
    latest = _rules().view(
        3,
        _state(
            phase="propose",
            leader=3,
            history=failed_records,
            agent_protocol=18,
        ),
        SeatThread(),
    )
    previous = _rules().view(
        3,
        _state(
            phase="propose",
            leader=3,
            history=failed_records,
            agent_protocol=17,
        ),
        SeatThread(),
    )

    assert "本任务若失败，红方立即获胜" in latest.prompt
    assert "任务席位是胜负资源，不是无成本查验" in latest.prompt
    assert "让嫌疑位再次执行任务不能洗清他" in latest.prompt
    assert "赛点" not in previous.prompt
    assert "任务席位是胜负资源" not in previous.prompt


def test_v19_percival_compares_candidates_and_keeps_claims_untrusted() -> None:
    history = [
        ProposalRecord(
            quest=2,
            attempt=1,
            leader_no=4,
            team=[2, 4, 5],
            votes={1: True, 2: True, 3: True, 4: True, 5: True},
            approved=True,
            result="fail",
            fails=2,
        )
    ]
    latest = _rules().view(
        2,
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            history=history,
            agent_protocol=19,
        ),
        SeatThread(),
    )
    previous = _rules().view(
        2,
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            history=history,
            agent_protocol=18,
        ),
        SeatThread(),
    )

    assert "两名夜视候选的公开任务履历对照" in latest.prompt
    assert "1号：尚未执行任务" in latest.prompt
    assert "5号：M2失败（2张破坏）" in latest.prompt
    assert "仙女的公开宣布" in latest.prompt
    assert "都只是玩家声明" in latest.prompt
    assert "持令牌执行查验的是查验者" in latest.prompt
    assert "目标才是被查验者" in latest.prompt
    assert "两名夜视候选的公开任务履历对照" not in previous.prompt
    assert "仙女的公开宣布" not in previous.prompt


def test_v20_ballot_does_not_request_an_unsupported_private_basis() -> None:
    old = _rules().view(
        2,
        _state(
            phase="team_vote",
            team=[1, 3],
            agent_protocol=19,
        ),
        SeatThread(),
    )
    fixed = _rules().view(
        2,
        _state(
            phase="team_vote",
            team=[1, 3],
            agent_protocol=20,
        ),
        SeatThread(),
    )

    assert "private_basis 必须区分主持人事实与玩家声明" in old.prompt
    assert "private_basis 必须区分主持人事实与玩家声明" not in fixed.prompt
    assert "调用 submit_ballot" in fixed.prompt
    assert "最终判断仍须区分主持人事实与玩家声明" in fixed.prompt
    assert "查验的是查验者，目标才是被查验者" in fixed.prompt


def test_v22_ballot_private_basis_is_required_and_never_projected() -> None:
    state = _state(
        phase="team_vote",
        team=[1, 3],
        agent_protocol=22,
    )
    raw = json.dumps(
        {
            "tool": {
                "name": "submit_ballot",
                "input": {
                    "approve": True,
                    "reason": "这支队伍符合我的最终判断。",
                    "private_basis": "我是蓝方；当前没有私有事实否定这支队伍。",
                },
            },
        },
        ensure_ascii=False,
    )

    parsed = _rules().parse(2, raw, state)
    action = json.loads(parsed.action)

    assert action["approve"] is True
    assert action["reason"] == "这支队伍符合我的最终判断。"
    assert "private_basis" not in action
    assert all("我是蓝方" not in delivery.text for delivery in parsed.deliveries)

    without_basis = json.dumps(
        {
            "tool": {
                "name": "submit_ballot",
                "input": {
                    "approve": True,
                    "reason": "这支队伍符合我的最终判断。",
                },
            },
        },
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="private_basis"):
        _rules().parse(2, without_basis, state)

    old = _rules().parse(
        2,
        without_basis,
        _state(
            phase="team_vote",
            team=[1, 3],
            agent_protocol=21,
        ),
    )
    assert json.loads(old.action)["approve"] is True

    view = _rules().view(2, state, SeatThread())
    assert "private_basis 中私下重核自己的阵营" in view.prompt


@pytest.mark.parametrize(
    ("seat_no", "membership"),
    [
        (1, "你本人在当前提议队伍中"),
        (2, "你本人不在当前提议队伍中"),
    ],
)
def test_v23_current_team_prompt_states_membership_without_deciding_vote(
    seat_no: int,
    membership: str,
) -> None:
    previous = _rules().view(
        seat_no,
        _state(
            phase="team_vote",
            team=[1, 3],
            agent_protocol=22,
        ),
        SeatThread(),
    )
    latest = _rules().view(
        seat_no,
        _state(
            phase="team_vote",
            team=[1, 3],
            agent_protocol=23,
        ),
        SeatThread(),
    )

    assert membership not in previous.prompt
    assert "在队或不在队本身都不能证明整队安全" not in previous.prompt
    assert "也不能单独决定赞成或反对" not in previous.prompt
    assert membership in latest.prompt
    assert "在队或不在队本身都不能证明整队安全" in latest.prompt
    assert "也不能单独决定赞成或反对" in latest.prompt


@pytest.mark.parametrize(
    ("agent_protocol", "expected_checks"),
    [
        (23, {}),
        (24, {2: {3: "good"}}),
    ],
)
def test_v24_lady_choice_persists_truth_only_for_the_holder(
    agent_protocol: int,
    expected_checks: dict[int, dict[int, str]],
) -> None:
    rules = _rules()
    state = _state(
        phase="lady_choose",
        lady_holder=2,
        lady_used=[2],
        agent_protocol=agent_protocol,
    )
    chosen = rules.parse(
        2,
        json.dumps(
            {
                "tool": {
                    "name": "submit_lady",
                    "input": {
                        "target_no": 3,
                        "private_basis": "我亲自选择查验3号。",
                    },
                },
            },
            ensure_ascii=False,
        ),
        state,
    )

    after = rules.apply(state, chosen.action)

    assert after.lady_seen == "good"
    assert after.lady_checks == expected_checks


def test_v24_lady_chain_replays_only_each_seats_direct_checks() -> None:
    rules = _rules()
    first = _state(
        phase="lady_choose",
        lady_holder=1,
        lady_used=[1],
        agent_protocol=24,
    )
    first_choice = rules.parse(
        1,
        json.dumps(
            {
                "tool": {
                    "name": "submit_lady",
                    "input": {
                        "target_no": 3,
                        "private_basis": "我亲自选择查验3号。",
                    },
                },
            },
            ensure_ascii=False,
        ),
        first,
    )
    after_first = rules.apply(first, first_choice.action)
    after_first_announce = rules.advance(rules.advance(after_first))

    second = after_first_announce.model_copy(
        deep=True,
        update={"phase": "lady_choose"},
    )
    second_choice = rules.parse(
        3,
        json.dumps(
            {
                "tool": {
                    "name": "submit_lady",
                    "input": {
                        "target_no": 2,
                        "private_basis": "我现在亲自选择查验2号。",
                    },
                },
            },
            ensure_ascii=False,
        ),
        second,
    )
    after_second = rules.apply(second, second_choice.action)
    public_claims = SeatThread(
        inbox=[
            "1号：「我查验3号是蓝方。」",
            "3号：「我查验2号是蓝方。」",
        ],
    )

    seat_1 = rules.view(
        1,
        after_second.model_copy(
            deep=True,
            update={"phase": "propose", "leader": 1},
        ),
        public_claims,
    )
    seat_2 = rules.view(
        2,
        after_second.model_copy(
            deep=True,
            update={"phase": "propose", "leader": 2},
        ),
        public_claims,
    )
    seat_3 = rules.view(
        3,
        after_second.model_copy(
            deep=True,
            update={"phase": "propose", "leader": 3},
        ),
        public_claims,
    )

    assert after_second.lady_checks == {
        1: {3: "good"},
        3: {2: "good"},
    }
    assert seat_1.lady_checks == {3: "good"}
    assert "你亲自从主持人收到的查验结果：[3号=蓝方]" in seat_1.prompt
    assert "你亲自从主持人收到的查验结果：[2号=蓝方]" not in seat_1.prompt
    assert "3号：「我查验2号是蓝方。」" in seat_1.prompt
    assert "其他玩家公开宣布的仙女结果全部只是玩家声明" in seat_1.prompt
    assert "不会沿令牌传递成你的确定事实" in seat_1.prompt

    assert seat_2.lady_checks == {}
    assert "你没有亲自从主持人收到过任何查验结果" in seat_2.prompt

    assert seat_3.lady_checks == {2: "good"}
    assert "你亲自从主持人收到的查验结果：[2号=蓝方]" in seat_3.prompt
    assert "你亲自从主持人收到的查验结果：[3号=蓝方]" not in seat_3.prompt

    previous = rules.view(
        1,
        after_second.model_copy(
            deep=True,
            update={
                "phase": "propose",
                "leader": 1,
                "agent_protocol": 23,
            },
        ),
        public_claims,
    )
    assert "主持人重申你的湖中仙女信息边界" not in previous.prompt
    assert "你亲自从主持人收到的查验结果" not in previous.prompt
    assert "你没有亲自从主持人收到过任何查验结果" not in previous.prompt


def test_v24_prompt_counts_prior_rejections_without_attempt_four_off_by_one() -> None:
    rules = _rules()
    attempt_4_state = _state(
        phase="team_vote",
        team=[1, 3],
        rejections=3,
        agent_protocol=24,
    )
    attempt_4 = rules.view(2, attempt_4_state, SeatThread())
    previous = rules.view(
        2,
        attempt_4_state.model_copy(update={"agent_protocol": 23}),
        SeatThread(),
    )
    attempt_5 = rules.view(
        2,
        attempt_4_state.model_copy(update={"rejections": 4}),
        SeatThread(),
    )

    assert "M1 第4次组队" in attempt_4.prompt
    assert "本任务此前已累计否决3次" in attempt_4.prompt
    assert "本提案若否决将进入第5次组队，不会立即判负" in attempt_4.prompt
    assert "这是第 5 次组队，再否决红方立即获胜" not in attempt_4.prompt

    assert "本任务此前已累计否决" not in previous.prompt
    assert "本提案若否决将进入第5次组队，不会立即判负" not in previous.prompt

    assert "M1 第5次组队" in attempt_5.prompt
    assert "本任务此前已累计否决4次" in attempt_5.prompt
    assert "这是第 5 次组队，再否决红方立即获胜" in attempt_5.prompt
    assert "不会立即判负" not in attempt_5.prompt


def test_v11_discussion_keeps_anonymous_cards_close_to_the_decision() -> None:
    view = _rules().view(
        2,
        _state(
            phase="discuss",
            team=[1, 3],
            discuss_index=0,
            agent_protocol=11,
        ),
        SeatThread(),
    )

    assert "任务牌匿名" in view.prompt
    assert "承诺或声称自己出成功" in view.prompt
    assert "不能据此向全桌自证" in view.prompt


def test_v11_banter_does_not_treat_anonymous_cards_as_revealed() -> None:
    view = _rules().view(
        1,
        _state(
            phase="banter",
            winner="evil",
            banter_index=0,
            agent_protocol=11,
        ),
        SeatThread(),
    )

    assert "系统至终局也不公布每人的任务牌" in view.prompt
    assert "只有破坏数和已知信息能够唯一推出时才能归因" in view.prompt
    assert "本阶段不调用任何工具" in view.prompt


def test_v21_banter_reasserts_role_and_keeps_revealed_knowledge_in_time() -> None:
    previous = _rules().view(
        1,
        _state(
            phase="banter",
            winner="evil",
            banter_index=0,
            agent_protocol=20,
        ),
        SeatThread(),
    )
    latest = _rules().view(
        1,
        _state(
            phase="banter",
            winner="evil",
            banter_index=0,
            agent_protocol=21,
        ),
        SeatThread(),
    )

    assert "你翻牌后的真实身份是梅林（蓝方）" in latest.prompt
    assert "只复盘一个由你亲自做出的关键决定" in latest.prompt
    assert "区分“当时已知”和“翻牌后才知道”" in latest.prompt
    assert "红方自己在队中时不能这样称呼" in latest.prompt
    assert "你翻牌后的真实身份" not in previous.prompt


def test_v2_view_uses_role_agent_minimal_prompt_and_exact_inbox() -> None:
    thread = SeatThread(
        inbox=["4号：「我反对这支队伍。」"],
        messages=[
            ThreadMessage(role="human", content="旧提问"),
            ThreadMessage(role="assistant", content='{"text":"旧回答"}'),
        ],
    )
    view = _rules().view(
        1,
        _state(phase="propose", leader=1, agent_protocol=2),
        thread,
        first_speak=True,
    )

    assert view.agent == "avalon-merlin"
    assert view.action_tool == "submit_team"
    assert view.speech_required is True
    assert view.schema is None
    assert "4号：「我反对这支队伍。」" in view.prompt
    assert "旧提问" not in view.prompt
    assert "旧提问" in view.seed_prompt and "旧回答" in view.seed_prompt
    assert "你的角色：梅林" in view.seed_prompt
    for distraction in ("观众", "tone", "strategy", "StructuredOutput", "monologue", "输出 JSON"):
        assert distraction not in view.prompt


def test_v11_view_replays_exact_public_mechanics_without_replacing_speech() -> None:
    record = ProposalRecord(
        quest=1,
        attempt=1,
        leader_no=1,
        team=[1, 3],
        votes={1: True, 2: False, 3: True, 4: True, 5: False},
        approved=True,
        fails=1,
        result="fail",
    )
    thread = SeatThread(inbox=["4号：「我仍然相信3号。」"])
    view = _rules().view(
        1,
        _state(
            phase="propose",
            leader=1,
            history=[record],
            agent_protocol=11,
        ),
        thread,
    )

    assert "4号：「我仍然相信3号。」" in view.prompt
    assert "主持人公开机械账本（确定事实" in view.prompt
    assert "队伍[1号、3号]" in view.prompt
    assert "1号赞成、2号反对、3号赞成、4号赞成、5号反对" in view.prompt
    assert "任务失败，1张破坏" in view.prompt

    v10 = _rules().view(
        1,
        _state(
            phase="propose",
            leader=1,
            history=[record],
            agent_protocol=10,
        ),
        thread,
    )
    assert "主持人公开机械账本" not in v10.prompt


def test_v12_indexes_current_team_task_history_without_inferring_side() -> None:
    history = [
        ProposalRecord(
            quest=1,
            attempt=1,
            leader_no=1,
            team=[1, 3],
            votes={1: True, 2: True, 3: True, 4: True, 5: True},
            approved=True,
            fails=1,
            result="fail",
        ),
        ProposalRecord(
            quest=2,
            attempt=1,
            leader_no=2,
            team=[2, 4, 5],
            votes={1: True, 2: True, 3: True, 4: True, 5: True},
            approved=True,
            fails=0,
            result="success",
        ),
    ]
    view = _rules().view(
        2,
        _state(
            phase="discuss",
            leader=1,
            team=[1, 2],
            history=history,
            discuss_index=0,
            agent_protocol=12,
        ),
        SeatThread(),
    )

    assert "主持人当前队伍公开任务履历索引" in view.prompt
    assert "参加失败任务不等于红方，未参加任务也不等于蓝方" in view.prompt
    assert "- 1号：M1失败（1张破坏）。" in view.prompt
    assert "- 2号：M2成功（0张破坏）。" in view.prompt
    assert "- 3号：" not in view.prompt.split("公开任务履历索引", 1)[1]


def test_v13_repeats_role_private_facts_next_to_current_team_decision() -> None:
    merlin = _rules().view(
        1,
        _state(
            phase="discuss",
            leader=5,
            team=[1, 4],
            discuss_index=0,
            agent_protocol=13,
        ),
        SeatThread(),
    )
    percival = _rules().view(
        2,
        _state(
            phase="discuss",
            leader=1,
            team=[1, 5],
            discuss_index=0,
            agent_protocol=13,
        ),
        SeatThread(),
    )
    assassin = _rules().view(
        4,
        _state(
            phase="discuss",
            leader=3,
            team=[4, 5],
            discuss_index=0,
            agent_protocol=13,
        ),
        SeatThread(),
    )

    assert "你是梅林；夜视确定红方是[4号、5号]" in merlin.prompt
    assert "当前队伍中的夜视红方：4号" in merlin.prompt
    assert "你是派西维尔；夜视候选[1号、5号]" in percival.prompt
    assert "当前队伍确定至少包含一名红方" in percival.prompt
    assert "你是刺客；开局确认同伙是[5号]" in assassin.prompt
    assert "当前队伍中的已知同伙：5号" in assassin.prompt


def test_v14_known_evil_team_receives_one_shared_default_saboteur() -> None:
    state = _state(
        phase="quest",
        team=[4, 5],
        votes={1: True, 2: True, 3: True, 4: True, 5: True},
        agent_protocol=14,
    )
    assassin = _rules().view(4, state, SeatThread())
    morgana = _rules().view(5, state, SeatThread())

    for view in (assassin, morgana):
        assert "本队互认红方[4号、5号]" in view.prompt
        assert "默认由座位最小的1人[4号]提交破坏" in view.prompt
    assert "你是本次默认破坏者" in assassin.prompt
    assert "你是本次默认隐藏者" in morgana.prompt


def test_v11_quest_ledger_includes_approved_current_proposal() -> None:
    view = _rules().view(
        1,
        _state(
            phase="quest",
            leader=1,
            team=[1, 3],
            votes={1: True, 2: False, 3: True, 4: True, 5: True},
            agent_protocol=11,
        ),
        SeatThread(inbox=["四票赞成、一票反对，提案通过。"]),
    )

    assert "尚无完成表决的提案" not in view.prompt
    assert "队伍[1号、3号]" in view.prompt
    assert "1号赞成、2号反对、3号赞成、4号赞成、5号赞成" in view.prompt
    assert "提案通过，任务进行中，破坏数尚未公开" in view.prompt


def test_v11_red_quest_prompt_repeats_only_known_teammates_on_team() -> None:
    no_known = _rules().view(
        4,
        _state(
            phase="quest",
            team=[1, 4],
            votes={1: True, 2: True, 3: True, 4: True, 5: True},
            agent_protocol=11,
        ),
        SeatThread(),
    )
    with_known = _rules().view(
        4,
        _state(
            phase="quest",
            team=[4, 5],
            votes={1: True, 2: True, 3: True, 4: True, 5: True},
            agent_protocol=11,
        ),
        SeatThread(),
    )

    assert "你开局确认认识且在本队的红方同伙：无" in no_known.prompt
    assert "可能包含你不认识的奥伯伦" in no_known.prompt
    assert "你开局确认认识且在本队的红方同伙：5号" in with_known.prompt


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
