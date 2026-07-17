"""games/sentences.py 句子流→旧 events 契约转换桥的测试。

覆盖：avalon/werewolf 两种玩法的全部事件类型、聚合事件（票型/任务结果/狼刀多数决）、
call_id 与新 TTS manifest 键（{seq}.{speaker}）同构、plugin.adapt 全量消化不抛、
library 发现新格式 replay、events.jsonl 优先于 sentences.jsonl。
"""

from __future__ import annotations

import json
from pathlib import Path


from server import replay
from server.games import sentences as bridge
from server.games.avalon.plugin import AvalonPlugin
from server.games.base import AdaptContext
from server.games.werewolf.plugin import WerewolfPlugin


def _sent(seq: int, turn_id: str, speaker: int | str, action: dict, text: str = "", channels: list | None = None) -> dict:
    deliveries = [{"text": text, "channels": channels or ["broadcast", "audience"]}] if text else []
    return {
        "seq": seq, "turn_id": turn_id, "speaker": speaker, "raw": "",
        "deliveries": deliveries, "action": json.dumps(action, ensure_ascii=False),
    }


def _ctx(replay_dir: Path) -> AdaptContext:
    return AdaptContext(replay_id="test/game", tts={}, names={}, host_name="司仪")


_AV_SEATS = [
    {"no": 1, "persona": "fox", "strategy": "prudent", "role": "merlin", "side": "good", "intel": [4, 5], "model": "m"},
    {"no": 2, "persona": "shy", "strategy": "prudent", "role": "servant", "side": "good", "intel": [], "model": "m"},
    {"no": 3, "persona": "diva", "strategy": "prudent", "role": "servant", "side": "good", "intel": [], "model": "m"},
    {"no": 4, "persona": "shade", "strategy": "prudent", "role": "assassin", "side": "evil", "intel": [5], "model": "m"},
    {"no": 5, "persona": "boss", "strategy": "prudent", "role": "minion", "side": "evil", "intel": [4], "model": "m"},
]


def _avalon_sentences() -> list[dict]:
    return [
        _sent(0, "pregame.opening", "host", {"kind": "game_start", "players": 5, "good": 3, "evil": 2, "seats": _AV_SEATS}, "欢迎来到《阿瓦隆》。"),
        _sent(1, "q1.a1.propose", 2, {"kind": "propose", "seat": 2, "team": [2, 3], "speech": "我带3号。", "monologue": "稳一手。"}),
        _sent(2, "q1.a1.discuss.0", "host", {"kind": "team_intro", "team": [2, 3]}, "从 1号 开始讨论。"),
        _sent(3, "q1.a1.discuss.0", 1, {"kind": "discuss", "seat": 1, "speech": "行吧。", "monologue": "看着。"}),
        _sent(4, "q1.a1.team_vote", 1, {"kind": "team_vote", "seat": 1, "approve": True, "monologue": "过。"}),
        _sent(5, "q1.a1.team_vote", 2, {"kind": "team_vote", "seat": 2, "approve": True, "monologue": "过。"}),
        _sent(6, "q1.a1.team_vote", 3, {"kind": "team_vote", "seat": 3, "approve": True, "monologue": "过。"}),
        _sent(7, "q1.a1.team_vote", 4, {"kind": "team_vote", "seat": 4, "approve": False, "monologue": "演。"}),
        _sent(8, "q1.a1.team_vote", 5, {"kind": "team_vote", "seat": 5, "approve": False, "monologue": "演。"}),
        _sent(9, "q1.a1.team_vote", "host", {"kind": "vote_result", "votes": {1: True, 2: True, 3: True, 4: False, 5: False}, "approved": True, "rejections": 0}, "三票赞成、两票反对，提案通过。"),
        _sent(10, "q1.a1.quest", 2, {"kind": "quest", "seat": 2, "success": True, "monologue": "稳。"}),
        _sent(11, "q1.a1.quest", 3, {"kind": "quest", "seat": 3, "success": True, "monologue": "稳。"}),
        _sent(12, "q1.a1.quest", "host", {"kind": "quest_result", "quest": 1, "fails": 0, "fails_required": 1, "result": "success"}, "第一个任务成功。"),
        _sent(13, "q2.a1.lady.choose", 1, {"kind": "lady_choose", "seat": 1, "target_no": 4, "monologue": "查他。"}),
        _sent(14, "q2.a1.lady.announce", "host", {"kind": "lady_transfer", "holder": 1, "target": 4}, "令牌传给 4号。"),
        _sent(15, "q2.a1.lady.announce", 1, {"kind": "lady_announce", "seat": 1, "speech": "4号是红方！", "monologue": "实锤。", "truth": "evil"}),
        _sent(16, "assassinate", 4, {"kind": "assassinate", "seat": 4, "target_no": 1, "speech": "就是你！", "monologue": "赌。", "hit": True}),
        _sent(17, "postgame.assassination", "host", {"kind": "assassination_result", "assassin": 4, "target": 1, "hit": True}, "刺杀命中，红方获胜。"),
        _sent(18, "postgame.reveal", "host", {"kind": "reveal", "winner": "evil", "win_reason": "assassination", "roles": {1: "merlin", 2: "servant", 3: "servant", 4: "assassin", 5: "minion"}}, "游戏结束，红方获胜。"),
        _sent(19, "banter.0", 1, {"kind": "banter", "seat": 1, "speech": "唉。", "monologue": "被秒了。"}),
        _sent(20, "postgame.closing", "host", {"kind": "closing"}, "本局结束，下一局见。"),
    ]


def _ww_sentences() -> list[dict]:
    seats = [
        {"no": i, "persona": "fox", "strategy": "prudent", "role": r, "side": s, "model": "m"}
        for i, (r, s) in enumerate([
            ("werewolf", "werewolf"), ("seer", "good"), ("witch", "good"), ("hunter", "good"),
            ("idiot", "good"), ("villager", "good"), ("villager", "good"), ("villager", "good"),
            ("villager", "good"), ("werewolf", "werewolf"), ("werewolf", "werewolf"), ("werewolf", "werewolf"),
        ], start=1)
    ]
    return [
        _sent(0, "pregame.opening", "host", {"kind": "game_start", "board": "b", "board_name": "预女猎白12人场", "players": 12, "wolves": 4, "gods": 4, "villagers": 4, "seats": seats}, "欢迎来到《狼人杀》。"),
        _sent(1, "day1.night_start", "host", {"kind": "night_start", "day": 1}, "第1天，天黑请闭眼。"),
        _sent(2, "day1.wolf.0", 1, {"kind": "wolf_kill", "seat": 1, "target_no": 6, "speech": "刀6。", "monologue": "随便。"}),
        _sent(3, "day1.wolf.1", 10, {"kind": "wolf_kill", "seat": 10, "target_no": 6, "speech": "跟。", "monologue": "跟。"}),
        _sent(4, "day1.wolf.2", 11, {"kind": "wolf_kill", "seat": 11, "target_no": 7, "speech": "刀7吧。", "monologue": "换。"}),
        _sent(5, "day1.wolf.3", 12, {"kind": "wolf_kill", "seat": 12, "target_no": 6, "speech": "还是6。", "monologue": "定。"}),
        _sent(6, "day1.seer", 2, {"kind": "seer_check", "seat": 2, "target_no": 1, "result": "werewolf", "monologue": "查头狼。"}),
        _sent(7, "day1.witch", 3, {"kind": "witch_action", "seat": 3, "save": False, "poison_target_no": None, "monologue": "省药。"}),
        _sent(8, "day1.dawn", "host", {"kind": "dawn", "day": 1, "deaths": [{"seat_no": 6, "role": "villager", "reason": "wolf_kill", "day": 1}]}, "昨夜倒下的是 6号。"),
        _sent(9, "day1.last_words.0", 6, {"kind": "last_words", "seat": 6, "speech": "我是平民。", "monologue": "冤。"}),
        _sent(10, "day1.discuss.0", 1, {"kind": "discuss", "seat": 1, "speech": "我是好人。", "monologue": "装。"}),
        _sent(11, "day1.vote", 1, {"kind": "vote", "seat": 1, "target_no": 2, "monologue": "投他。"}),
        _sent(12, "day1.vote", 2, {"kind": "vote", "seat": 2, "target_no": 1, "monologue": "投他。"}),
        _sent(13, "day1.vote", 3, {"kind": "vote", "seat": 3, "target_no": 1, "monologue": "跟。"}),
        _sent(14, "day1.vote_result", "host", {"kind": "vote_result", "day": 1, "votes": {1: 2, 2: 1, 3: 1}, "exiled": 1, "tied": False}, "1号 被放逐出局，其身份是狼人。"),
        _sent(15, "day1.last_words.0", 1, {"kind": "last_words", "seat": 1, "speech": "我不是狼。", "monologue": "嘴硬。"}),
        _sent(16, "day2.night_start", "host", {"kind": "night_start", "day": 2}, "第2天，天黑请闭眼。"),
        _sent(17, "day2.wolf.0", 10, {"kind": "wolf_kill", "seat": 10, "target_no": 4, "speech": "刀猎人。", "monologue": "拼。"}),
        _sent(18, "day2.wolf.1", 11, {"kind": "wolf_kill", "seat": 11, "target_no": 4, "speech": "跟。", "monologue": "跟。"}),
        _sent(19, "day2.wolf.2", 12, {"kind": "wolf_kill", "seat": 12, "target_no": 4, "speech": "跟。", "monologue": "跟。"}),
        _sent(20, "day2.dawn", "host", {"kind": "dawn", "day": 2, "deaths": [{"seat_no": 4, "role": "hunter", "reason": "wolf_kill", "day": 2}]}, "昨夜倒下的是 4号。"),
        _sent(21, "day2.hunter", 4, {"kind": "hunter_shot", "seat": 4, "target_no": 10, "speech": "带走10号。", "monologue": "蒙的。"}),
        _sent(22, "day2.hunter_result", "host", {"kind": "hunter_shot_result", "hunter": 4, "target_no": 10, "victim_role": "werewolf"}, "猎人 4号 开枪带走 10号，其身份是狼人。"),
        _sent(23, "day2.discuss.0", 2, {"kind": "discuss", "seat": 2, "speech": "1是狼，10也是狼。", "monologue": "查杀。"}),
        _sent(24, "day2.vote", 2, {"kind": "vote", "seat": 2, "target_no": 11, "monologue": "投。"}),
        _sent(25, "day2.vote", 3, {"kind": "vote", "seat": 3, "target_no": 11, "monologue": "投。"}),
        _sent(26, "day2.vote_result", "host", {"kind": "vote_result", "day": 2, "votes": {2: 11, 3: 11}, "exiled": 11, "tied": False}, "11号 被放逐出局，其身份是狼人。"),
        _sent(27, "day3.night_start", "host", {"kind": "night_start", "day": 3}, "第3天，天黑请闭眼。"),
        _sent(28, "day3.wolf.0", 12, {"kind": "wolf_kill", "seat": 12, "target_no": 2, "speech": "刀预言家。", "monologue": "完了。"}),
        _sent(29, "day3.dawn", "host", {"kind": "dawn", "day": 3, "deaths": [{"seat_no": 2, "role": "seer", "reason": "wolf_kill", "day": 3}]}, "昨夜倒下的是 2号。"),
        _sent(30, "day3.last_words.0", 2, {"kind": "last_words", "seat": 2, "speech": "1、10、11都是狼。", "monologue": "报完了。"}),
        _sent(31, "day3.discuss.0", 3, {"kind": "discuss", "seat": 3, "speech": "那就剩12号了。", "monologue": "明朗。"}),
        _sent(32, "day3.vote", 3, {"kind": "vote", "seat": 3, "target_no": 12, "monologue": "投。"}),
        _sent(33, "day3.vote_result", "host", {"kind": "vote_result", "day": 3, "votes": {3: 12}, "exiled": 12, "tied": False}, "12号 被放逐出局，其身份是狼人。"),
        _sent(34, "postgame.reveal", "host", {"kind": "reveal", "winner": "good", "win_reason": "all_wolves_dead", "roles": {}, "deaths": []}, "游戏结束，好人获胜。"),
        _sent(35, "banter.0", 12, {"kind": "banter", "seat": 12, "speech": "队友卖得干净。", "monologue": "服。"}),
        _sent(36, "postgame.closing", "host", {"kind": "closing"}, "本局结束，下一局见。"),
    ]


def _write_game(root: Path, name: str, sentences: list[dict], meta: dict) -> Path:
    d = root / name
    d.mkdir(parents=True)
    d.joinpath("sentences.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in sentences), encoding="utf-8")
    d.joinpath("meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return d


# ---------------------------------------------------------------- Avalon

def test_avalon_bridge_covers_every_event_type(tmp_path):
    events = bridge._avalon_events(_avalon_sentences(), {"setup": {"first_leader": 2, "lady_holder": 1}})
    types = {e["type"] for e in events}
    assert types == {"game_start", "host", "deal", "propose", "discuss", "vote_result", "quest_result",
                     "lady", "assassination", "game_end", "banter"}
    plugin = AvalonPlugin()
    ctx = _ctx(tmp_path)
    for e in events:  # 契约硬保证：plugin 逐条消化不抛（未知/缺字段立刻暴露）
        assert plugin.adapt(e, ctx) is not None or (e["type"] == "host" and e["beat"] == "reveal")


def test_avalon_bridge_aggregates_votes_and_cards(tmp_path):
    events = bridge._avalon_events(_avalon_sentences(), {"setup": {}})
    vr = next(e for e in events if e["type"] == "vote_result")
    assert [(v["seat_no"], v["approve"]) for v in vr["votes"]] == [(1, True), (2, True), (3, True), (4, False), (5, False)]
    assert vr["votes"][0]["call_id"] == "4.1"  # 与 manifest 键 {seq}.{speaker} 同构
    assert vr["approved"] is True and vr["rejections"] == 0
    qr = next(e for e in events if e["type"] == "quest_result")
    assert qr["team"] == [2, 3] and qr["result"] == "success" and qr["succeeded"] == 1
    assert [(c["seat_no"], c["success"]) for c in qr["cards"]] == [(2, True), (3, True)]


def test_avalon_bridge_lady_and_meta_deal(tmp_path):
    events = bridge._avalon_events(_avalon_sentences(), {"setup": {"first_leader": 2, "lady_holder": 1}})
    deal = next(e for e in events if e["type"] == "deal")
    assert deal["leader_no"] == 2 and deal["lady_holder"] == 1 and len(deal["seats"]) == 5
    lady = next(e for e in events if e["type"] == "lady")
    assert lady["holder_no"] == 1 and lady["target_no"] == 4 and lady["truth"] == "evil"
    assert lady["choose_call_id"] == "13.1" and lady["announce_call_id"] == "15.1"
    ge = next(e for e in events if e["type"] == "game_end")
    assert ge["winner"] == "evil" and len(ge["seats"]) == 5


# ---------------------------------------------------------------- Werewolf

def test_werewolf_bridge_covers_every_event_type(tmp_path):
    events = bridge._werewolf_events(_ww_sentences(), {})
    types = {e["type"] for e in events}
    assert types == {"game_start", "host", "deal", "night_start", "wolf_kill", "seer_check", "witch_action",
                     "death_resolved", "role_revealed", "speech", "vote_result", "hunter_shot", "game_end", "banter"}
    plugin = WerewolfPlugin()
    ctx = _ctx(tmp_path)
    for e in events:
        assert plugin.adapt(e, ctx) is not None or (e["type"] == "host" and e["beat"] == "reveal")


def test_werewolf_bridge_wolf_kill_majority(tmp_path):
    events = bridge._werewolf_events(_ww_sentences(), {})
    wk = next(e for e in events if e["type"] == "wolf_kill")
    assert wk["day"] == 1 and wk["target_no"] == 6  # 3:1 多数
    assert [v["seat_no"] for v in wk["votes"]] == [1, 10, 11, 12]
    assert wk["votes"][1]["call_id"] == "3.10"
    # 事件位置：狼刀聚合在天亮（death_resolved）之前
    assert events.index(wk) < next(i for i, e in enumerate(events) if e["type"] == "death_resolved")


def test_werewolf_bridge_hunter_chain_and_game_end(tmp_path):
    events = bridge._werewolf_events(_ww_sentences(), {})
    hs = next(e for e in events if e["type"] == "hunter_shot")
    assert hs["hunter_no"] == 4 and hs["target_no"] == 10
    rr = [e for e in events if e["type"] == "role_revealed"]
    assert any(e["seat_no"] == 10 and e["role"] == "werewolf" and e["reason"] == "hunter_shot" for e in rr)
    ge = next(e for e in events if e["type"] == "game_end")
    assert ge["winner"] == "good" and ge["win_reason"] == "all_wolves_dead"
    alive = {s["no"]: s["alive"] for s in ge["seats"]}
    assert alive[1] is False and alive[10] is False and alive[12] is False and alive[3] is True


# ---------------------------------------------------------------- replay.py 集成

def test_load_events_prefers_old_format(tmp_path):
    d = _write_game(tmp_path, "g", _avalon_sentences(), {"game": "avalon", "setup": {}})
    d.joinpath("events.jsonl").write_text('{"type": "game_start", "players": 5}\n', encoding="utf-8")
    events = replay.load_events(d)
    assert events == [{"type": "game_start", "players": 5}]  # 旧格式优先，桥不接管


def test_library_discovers_sentences_replay(tmp_path):
    _write_game(tmp_path, "av1", _avalon_sentences(), {"game": "avalon", "setup": {}})
    _write_game(tmp_path, "ww1", _ww_sentences(), {"game": "werewolf", "setup": {}})
    items = replay.library(tmp_path)
    by_id = {i.id: i.game for i in items}
    assert by_id == {"av1": "avalon", "ww1": "werewolf"}


def test_crashed_sentences_replay_is_not_broadcastable(tmp_path):
    d = _write_game(tmp_path, "half", _avalon_sentences()[:10], {"game": "avalon", "setup": {}})
    assert replay.is_broadcastable(d) is False  # 无 game_end（崩局拦在库外）
    assert replay.is_broadcastable(_write_game(tmp_path, "full", _avalon_sentences(), {"game": "avalon", "setup": {}})) is True
