"""adapter 与 reducer 的不变量：全事件类型覆盖、未知类型即抛、状态折叠正确、feed 窗口护栏。"""

from __future__ import annotations

import json
import time

import pytest

from server import scene as avalon
from server.protocol import FeedItem, Scene
from server.tests.conftest import EVENTS, write_replay


def _adapt_all(replay_root):
    d = replay_root / "game1"
    host = avalon.load_host_name(d)
    tts = avalon.load_tts(d)
    names = avalon.load_names(d)
    return [avalon.adapt(e, replay_id="game1", tts=tts, names=names, host_name=host) for e in EVENTS]


def _fold(directives) -> Scene:
    scene = Scene()
    for seq, d in enumerate(directives, start=1):
        avalon.apply(scene, FeedItem(seq=seq, directive=d, started_at=time.time(), duration=1.0))
    return scene


def test_adapt_covers_every_event_type(replay_root):
    directives = _adapt_all(replay_root)
    assert len(directives) == len(EVENTS)
    kinds = [d.kind for d in directives]
    assert kinds == ["game_open", "host", "deal", "speech", "speech", "vote_reveal",
                     "quest_reveal", "speech", "assassination", "game_over", "speech"]
    assert directives[1].name == "司仪"  # meta.setup.host_name（生成时快照，不读 cast.yaml）


def test_adapt_unknown_event_raises(replay_root):
    with pytest.raises(ValueError, match="未知事件类型"):
        avalon.adapt({"type": "nova"}, replay_id="g", tts={}, names={})


def test_persona_names_resolved(replay_root):
    deal = _adapt_all(replay_root)[2]
    assert [s.name for s in deal.seats] == ["老狐狸", "社恐", "戏精", "阴阳师", "大姐头"]


def test_directives_never_carry_replay_id(replay_root):
    # 隐私边界：公开频道（directive 是它的载荷）不出现 replay 目录名
    for d in _adapt_all(replay_root):
        assert "replay_id" not in d.model_dump()


def test_state_folds_full_game(replay_root):
    scene = _fold(_adapt_all(replay_root))
    assert len(scene.seats) == 5
    assert scene.missions[0] == "fail"
    assert scene.rejections == 0
    assert scene.team == []  # quest 开牌后清队
    assert scene.lady_holder == 2  # 仙女令牌传给被查者
    assert scene.winner == "evil" and scene.win_reason == "assassination"
    assert scene.speaking == 1  # 最后一拍是 1 号的 banter


def test_feed_capped(replay_root):
    scene = _fold(_adapt_all(replay_root))
    assert len(scene.feed) == avalon.FEED_CAP


def test_timeline_accumulates_milestones(replay_root):
    # 前情回顾派生态：提队入账（leader/team 取自刚才的 propose 拍）+ 任务入账（god-view 标出破坏者）。
    scene = _fold(_adapt_all(replay_root))
    kinds = [t.kind for t in scene.timeline]
    assert kinds == ["proposal", "quest"]
    prop, quest = scene.timeline
    assert (prop.quest, prop.attempt, prop.leader_no, prop.team, prop.approved) == (1, 1, 2, [2, 3], True)
    assert prop.approves == [1, 2, 3, 4, 5] and prop.rejects == []
    assert (quest.quest, quest.team, quest.result, quest.fails) == (1, [2, 3], "fail", 1)
    assert quest.fail_seats == [2]  # 2 号出了破坏票（观众上帝视角看得到，公开只报张数）


def test_game_open_resets_timeline(replay_root):
    directives = _adapt_all(replay_root)
    scene = _fold(directives)
    avalon.apply(scene, FeedItem(seq=99, directive=directives[0], started_at=time.time(), duration=1.0))
    assert scene.timeline == []


def test_game_open_resets_previous_game(replay_root):
    directives = _adapt_all(replay_root)
    scene = _fold(directives)
    avalon.apply(scene, FeedItem(seq=99, directive=directives[0], started_at=time.time(), duration=1.0))
    assert scene.winner is None and scene.missions == [None] * 5 and scene.seats == []


def _write_manifest(d, entries):
    (d / "tts").mkdir(exist_ok=True)
    (d / "tts" / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")


def test_audio_url_from_manifest(replay_root):
    d = replay_root / "game1"
    _write_manifest(d, {
        "1.1.propose.2": {"file": "1.1.propose.2.wav", "duration_ms": 4500},
        "1.1.host.opening": {"file": "1.1.host.opening.wav", "duration_ms": 3000},
    })
    directives = _adapt_all(replay_root)
    assert directives[3].audio_url == "/replays/game1/tts/1.1.propose.2.wav"
    assert directives[1].audio_url == "/replays/game1/tts/1.1.host.opening.wav"  # 主持人同样按 call_id 对齐
    assert directives[4].audio_url is None  # manifest 里没有就不带音频（无声照常播）


def test_load_tts_missing_is_empty(replay_root):
    # 没有 TTS 的老 replay：空表，发言回退字数估算、不带音频
    bare = write_replay(replay_root, "no-tts", voiced=False)
    assert avalon.load_tts(bare) == {}


def test_host_name_fallback(tmp_path):
    bare = tmp_path / "old-replay"
    bare.mkdir()
    assert avalon.load_host_name(bare) == "主持人"  # 老 replay 无 meta 也不炸
    assert avalon.load_names(bare) == {}  # 同样优雅降级：没 meta 就空表，座位回退裸 id
