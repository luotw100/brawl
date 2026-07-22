"""AvalonRules 阶段机推进：计票、否决上限、任务轨道、刺杀入口、pending 单发纪律。"""

from __future__ import annotations

import json
from typing import ClassVar

from avalon import rules as av_rules
from avalon.rules_engine import AvalonRules, AvalonState
from avalon.state import Role, Seat
from core.sr.loop import deliver
from core.sr.tts import TTSRenderer
from core.sr.types import Sentence
from core.threads import SeatThread

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


def test_three_succeeded_quests_triggers_full_evil_chat_before_assassination() -> None:
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
    assert st.phase == "evil_chat"
    assert st.winner is None  # 刺杀前胜负未定
    # 完整红方名单作为终局披露信息独立保存，不篡改开局夜视。
    assert st.evil_team == [4, 5]
    assert st._seat(4).intel == [5]
    assert st._seat(5).intel == [4]
    exp = r.expected(st)
    assert exp.seats == [4]
    st = r.advance(r.apply(st, json.dumps({"kind": "evil_chat", "seat": 4})))
    assert r.expected(st).seats == [5]
    st = r.advance(r.apply(st, json.dumps({"kind": "evil_chat", "seat": 5})))
    assert st.phase == "assassinate"
    assert r.expected(st).seats == [4]


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


def test_vote_result_tts_reads_summary_while_all_inboxes_receive_vote_ledger(tmp_path) -> None:
    r = _rules()
    st = _state(phase="team_vote", team=[1, 2], pregame_done=True)
    emit = r.pending(_vote_all(r, st, approve=True))[0]

    assert len(emit.deliveries) == 2
    ledger, spoken = emit.deliveries
    assert ledger.text.startswith("【开票】")
    assert ledger.spoken is False
    assert "提案通过" in spoken.text and not spoken.text.startswith("【开票】")
    assert spoken.spoken is True

    threads = {no: SeatThread() for no in range(1, 6)}
    deliver(emit.deliveries, threads)
    assert all(ledger.text in threads[no].inbox for no in threads)

    class FakeTTS:
        name: ClassVar[str] = "fake"

        async def synthesize(self, text: str, voice_id: str) -> bytes:
            raise AssertionError("this test only projects utterances")

    utterances = TTSRenderer(FakeTTS(), tmp_path)._utterances(
        [
            Sentence(
                seq=7,
                turn_id="vote",
                speaker="host",
                deliveries=emit.deliveries,
            )
        ],
        [f"player-{no}" for no in range(1, 6)],
        "host-voice",
    )
    assert len(utterances) == 1
    assert utterances[0].text == spoken.text and utterances[0].voice_id == "host-voice"


def test_pregame_opening_emitted_once() -> None:
    r = _rules()
    st = _state()
    batch = r.pending(st)
    assert len(batch) == 1 and json.loads(batch[0].action)["kind"] == "game_start"
    # 首个行动 apply 后开场播报不再重复
    st = r.apply(st, json.dumps({"kind": "propose", "seat": 1, "team": [1, 2], "speech": "s", "monologue": "m"}))
    assert all(json.loads(e.action)["kind"] != "game_start" for e in r.pending(st))


def test_lady_transfer_is_applied_before_host_announces_it() -> None:
    r = _rules()
    st = _state(
        phase="lady_announce",
        pregame_done=True,
        lady_holder=2,
        lady_used=[2],
        lady_target=4,
        lady_seen="evil",
    )
    announced = r.apply(
        st,
        json.dumps(
            {
                "kind": "lady_announce",
                "seat": 2,
                "speech": "四号是红方",
                "monologue": "如实说",
            }
        ),
    )
    # 玩家宣布本身还没有声称令牌转移。
    assert announced.lady_holder == 2
    assert r.pending(announced) == []

    transferred = r.advance(announced)
    assert transferred.lady_holder == 4
    assert transferred.lady_used == [2, 4]
    emit = r.pending(transferred)
    assert len(emit) == 1
    assert json.loads(emit[0].action) == {
        "kind": "lady_transfer",
        "holder": 2,
        "target": 4,
    }
    assert "令牌由二号传给四号" in emit[0].deliveries[0].text
    assert "查验四号" not in emit[0].deliveries[0].text


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
