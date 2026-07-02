"""阿瓦隆音效 cue DSL 单测：锁住一拍→PlayPlan sfx tracks 的映射。"""

from __future__ import annotations

from server.games.avalon.protocol import (
    Assassination,
    Deal,
    GameOpen,
    GameOver,
    LadyCheck,
    QuestReveal,
    SeatInfo,
    SeatVote,
    Speech,
    VoteReveal,
)
from server.playplan import compile_stage_beats, load_plan, plan_path
from server.protocol import HostSpeech, Intermission

PLAN = load_plan(plan_path("avalon"))


def fallback_text_window(*parts: str) -> float:
    return len("".join(parts)) / PLAN.fallback_cps + PLAN.audio_margin


def sfx_for(directive) -> list[tuple[str | None, float]]:
    beats = compile_stage_beats([directive], PLAN, {}, speed=1.0)
    if not beats:
        return []
    return [(t.name, t.start) for t in beats[0].tracks if t.kind == "sfx"]


def test_simple_beats_one_cue_at_zero():
    assert sfx_for(GameOpen(players=6, good=4, evil=2)) == [("open", 0.0)]


def test_deal_has_no_sfx():
    # 发牌不配音效（"咔咔"声刺耳）——画面照常飞牌，但无任何 cue。
    seats = [SeatInfo(no=i, name=f"P{i}", persona=f"p{i}", role="servant", side="good", model="m") for i in range(1, 4)]
    assert sfx_for(Deal(seats=seats, leader_no=1, lady_holder=None)) == []
    assert sfx_for(Deal(seats=[], leader_no=1, lady_holder=None)) == []


def test_quest_and_game_over_branch_on_outcome():
    ok = QuestReveal(quest=1, team=[1, 2], cards=[], fails=0, fails_required=1, result="success")
    bad = ok.model_copy(update={"result": "fail"})
    assert sfx_for(ok) == [("quest-success", 0.0)]
    assert sfx_for(bad) == [("quest-fail", 0.0)]
    win = GameOver(winner="good", win_reason="三战全胜", seats=[])
    assert sfx_for(win) == [("win-good", 0.0)]
    assert sfx_for(win.model_copy(update={"winner": "evil"})) == [("win-evil", 0.0)]


def test_assassination_branches_on_hit():
    a = Assassination(assassin_no=1, target_no=2, hit=True, text="就是你。", monologue="")
    expected = fallback_text_window("就是你。") * 0.8
    assert sfx_for(a) == [("assassin-hit", expected)]
    assert sfx_for(a.model_copy(update={"hit": False})) == [("assassin-miss", expected)]


def test_lady_check_has_intro_and_result_cues():
    d = LadyCheck(holder_no=1, target_no=2, truth="evil", choose_monologue="")
    assert sfx_for(d) == [("open", 0.0), ("stamp", 4.0 * 0.54)]


def test_speech_think_only_when_monologue():
    voiced = Speech(phase="discuss", seat_no=1, text="hi", monologue="盘算中")
    silent = voiced.model_copy(update={"monologue": ""})
    assert sfx_for(voiced) == [("think", 0.0)]
    assert sfx_for(silent) == []


def test_vote_reveal_staggered_stamps_then_result():
    votes = [SeatVote(seat_no=i, approve=True, monologue="") for i in range(1, 5)]
    cues = sfx_for(VoteReveal(quest=1, attempt=1, votes=votes, approved=True, rejections=0))
    duration = 4 * 0.8 + 2.5
    stagger = duration * 0.62 / 4
    assert cues == [
        ("stamp", 0.0), ("stamp", stagger), ("stamp", 2 * stagger), ("stamp", 3 * stagger),
        ("vote-pass", 4 * stagger),
    ]
    # 否决走 vote-fail
    rejected = sfx_for(VoteReveal(quest=1, attempt=1, votes=votes[:1], approved=False, rejections=3))
    rejected_duration = 1 * 0.8 + 2.5
    assert rejected[-1] == ("vote-fail", 1 * (rejected_duration * 0.62 / 1))


def test_silent_beats_have_no_cue():
    assert sfx_for(HostSpeech(name="主持", beat="opening", text="欢迎")) == []
    assert sfx_for(Intermission()) == []
