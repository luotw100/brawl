"""音效 cue 表单测：锁住一拍→音效映射（这张表必须与 web/src/stage/sfx.ts 手动同步）。"""

from __future__ import annotations

from server.protocol import (
    Assassination,
    Deal,
    GameOpen,
    GameOver,
    HostSpeech,
    Intermission,
    QuestReveal,
    SeatVote,
    Speech,
    VoteReveal,
)
from server.sfx import cues_for


def test_simple_beats_one_cue_at_zero():
    assert cues_for(GameOpen(players=6, good=4, evil=2), 1.0) == [("open", 0.0)]
    assert cues_for(Deal(seats=[], leader_no=1, lady_holder=None), 1.0) == [("deal", 0.0)]


def test_quest_and_game_over_branch_on_outcome():
    ok = QuestReveal(quest=1, team=[1, 2], cards=[], fails=0, fails_required=1, result="success")
    bad = ok.model_copy(update={"result": "fail"})
    assert cues_for(ok, 1.0) == [("quest-success", 0.0)]
    assert cues_for(bad, 1.0) == [("quest-fail", 0.0)]
    win = GameOver(winner="good", win_reason="三战全胜", seats=[])
    assert cues_for(win, 1.0) == [("win-good", 0.0)]
    assert cues_for(win.model_copy(update={"winner": "evil"}), 1.0) == [("win-evil", 0.0)]


def test_assassination_branches_on_hit():
    a = Assassination(assassin_no=1, target_no=2, hit=True, text="", monologue="")
    assert cues_for(a, 1.0) == [("assassin-hit", 0.0)]
    assert cues_for(a.model_copy(update={"hit": False}), 1.0) == [("assassin-miss", 0.0)]


def test_speech_think_only_when_monologue():
    voiced = Speech(phase="discuss", seat_no=1, text="hi", monologue="盘算中")
    silent = voiced.model_copy(update={"monologue": ""})
    assert cues_for(voiced, 1.0) == [("think", 0.0)]
    assert cues_for(silent, 1.0) == []


def test_vote_reveal_staggered_stamps_then_result():
    votes = [SeatVote(seat_no=i, approve=True, monologue="") for i in range(1, 5)]
    cues = cues_for(VoteReveal(quest=1, attempt=1, votes=votes, approved=True, rejections=0), 10.0)
    stagger = 10.0 * 0.62 / 4  # 1.55s
    assert cues == [
        ("stamp", 0.0), ("stamp", stagger), ("stamp", 2 * stagger), ("stamp", 3 * stagger),
        ("vote-pass", 4 * stagger),
    ]
    # 否决走 vote-fail
    rejected = cues_for(VoteReveal(quest=1, attempt=1, votes=votes[:1], approved=False, rejections=3), 10.0)
    assert rejected[-1] == ("vote-fail", 1 * (10.0 * 0.62 / 1))


def test_silent_beats_have_no_cue():
    assert cues_for(HostSpeech(name="主持", beat="opening", text="欢迎"), 1.0) == []
    assert cues_for(Intermission(), 1.0) == []
