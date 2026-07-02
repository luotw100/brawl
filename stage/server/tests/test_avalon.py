"""阿瓦隆插件的不变量：全事件类型覆盖、未知类型即抛、状态折叠正确、feed 窗口护栏。"""

from __future__ import annotations

import json
import time

import pytest

from server import replay
from server.engine import FEED_CAP
from server.games.avalon.plugin import AvalonPlugin
from server.games.avalon.protocol import AvalonScene
from server.games.base import AdaptContext
from server.protocol import FeedItem, StateMsg
from server.tests.conftest import EVENTS, write_replay

PLUGIN = AvalonPlugin()


def _ctx(replay_dir, replay_id="game1"):
    return AdaptContext(
        replay_id=replay_id,
        tts=replay.load_tts(replay_dir),
        names=replay.load_names(replay_dir),
        host_name=replay.load_host_name(replay_dir),
    )


def _adapt_all(replay_root):
    d = replay_root / "game1"
    ctx = _ctx(d)
    return [PLUGIN.adapt(e, ctx) for e in EVENTS]


def _fold_one(scene: AvalonScene, directive, seq: int = 1) -> None:
    """复刻引擎的一拍折叠：feed 入队 + 窗口护栏（玩法无关，引擎做）→ 玩法 reducer。"""
    item = FeedItem(seq=seq, directive=directive, started_at=time.time(), duration=1.0)
    scene.feed.append(item)
    del scene.feed[:-FEED_CAP]
    PLUGIN.apply(scene, item)


def _fold(directives) -> AvalonScene:
    scene = PLUGIN.new_scene()
    for seq, d in enumerate(directives, start=1):
        _fold_one(scene, d, seq)
    return scene


def test_adapt_covers_every_event_type(replay_root):
    directives = _adapt_all(replay_root)
    assert len(directives) == len(EVENTS)
    kinds = [d.kind for d in directives]
    assert kinds == ["game_open", "host", "deal", "speech", "speech", "vote_reveal",
                     "quest_reveal", "lady_reveal", "assassination", "game_over", "speech"]
    assert directives[1].name == "司仪"  # meta.setup.host_name（生成时快照，不读 cast.yaml）


def test_adapt_unknown_event_raises():
    with pytest.raises(ValueError, match="未知事件类型"):
        PLUGIN.adapt({"type": "nova"}, AdaptContext(replay_id="g", tts={}, names={}))


def test_host_reveal_beat_not_staged():
    # 上帝视角全程亮身份，主持人复述身份的 reveal 拍不演 → adapt 返回 None（其余 host 拍照常出 directive）
    ctx = AdaptContext(replay_id="g", tts={}, names={})
    reveal = {"type": "host", "beat": "reveal", "call_id": "5.1.host.reveal", "text": "现在公布全部身份：……"}
    assert PLUGIN.adapt(reveal, ctx) is None
    closing = {"type": "host", "beat": "closing", "call_id": "5.1.host.closing", "text": "下一局再见。"}
    assert PLUGIN.adapt(closing, ctx) is not None


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
    assert len(scene.feed) == FEED_CAP


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


def test_timeline_serializes_concrete_fields_for_stage(replay_root):
    # 回归：插件化后 timeline 一度按 TimelineEntry 基类序列化，只剩 {"kind": "..."}；
    # 前端前情提要读取 team/approves/rejects 时会崩成黑屏。
    scene = _fold(_adapt_all(replay_root))
    dumped = StateMsg(phase="playing", scene=scene, overlays=[]).model_dump(mode="json")
    proposal, quest = dumped["scene"]["timeline"]
    assert proposal == {
        "kind": "proposal",
        "quest": 1,
        "attempt": 1,
        "leader_no": 2,
        "team": [2, 3],
        "approved": True,
        "approves": [1, 2, 3, 4, 5],
        "rejects": [],
    }
    assert quest == {
        "kind": "quest",
        "quest": 1,
        "team": [2, 3],
        "result": "fail",
        "fails": 1,
        "fail_seats": [2],
    }


def test_game_open_resets_timeline(replay_root):
    directives = _adapt_all(replay_root)
    scene = _fold(directives)
    _fold_one(scene, directives[0], seq=99)
    assert scene.timeline == []


def test_game_open_resets_previous_game(replay_root):
    directives = _adapt_all(replay_root)
    scene = _fold(directives)
    _fold_one(scene, directives[0], seq=99)
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
    assert replay.load_tts(bare) == {}


def test_host_name_fallback(tmp_path):
    bare = tmp_path / "old-replay"
    bare.mkdir()
    assert replay.load_host_name(bare) == "主持人"  # 老 replay 无 meta 也不炸
    assert replay.load_names(bare) == {}  # 同样优雅降级：没 meta 就空表，座位回退裸 id
