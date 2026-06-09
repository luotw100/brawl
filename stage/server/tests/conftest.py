"""测试公共底座：用 games 的 random 后端思路——不依赖任何真 replay，自己造最小快照。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.settings import StageSettings

# 一局覆盖全部 10 种事件类型的微型 replay（手工裁剪，形状与 avalon/nodes.py 的 emit 一致）
EVENTS = [
    {"type": "game_start", "players": 5, "good": 3, "evil": 2},
    {"type": "host", "beat": "opening", "call_id": "1.1.host.opening", "text": "欢迎来到《阿瓦隆》。"},
    {"type": "deal", "leader_no": 2, "lady_holder": 1, "seats": [
        {"no": i, "persona": p, "strategy": "standard", "role": r, "side": s, "intel": [], "model": "m"}
        for i, (p, r, s) in enumerate([
            ("fox", "merlin", "good"), ("shy", "assassin", "evil"), ("diva", "servant", "good"),
            ("shade", "minion", "evil"), ("boss", "servant", "good")], start=1)
    ]},
    {"type": "propose", "quest": 1, "attempt": 1, "call_id": "1.1.propose.2", "leader_no": 2,
     "team": [2, 3], "speech": "我带3号。", "monologue": "稳一手。"},
    {"type": "discuss", "quest": 1, "attempt": 1, "call_id": "1.1.discuss.3", "seat_no": 3,
     "text": "行吧。", "monologue": "没看出问题。"},
    {"type": "vote_result", "quest": 1, "attempt": 1, "approved": True, "rejections": 0, "votes": [
        {"seat_no": i, "approve": True, "monologue": "过", "call_id": f"1.1.team_vote.{i}"} for i in range(1, 6)
    ]},
    {"type": "quest_result", "quest": 1, "team": [2, 3], "cards": [
        {"seat_no": 2, "success": False, "monologue": "动手。"},
        {"seat_no": 3, "success": True, "monologue": "稳。"}],
     "fails": 1, "fails_required": 1, "result": "fail", "succeeded": 1, "failed": 1},
    {"type": "lady", "holder_no": 1, "target_no": 2, "truth": "evil",
     "choose_call_id": "2.1.lady_choose.1", "announce_call_id": "2.1.lady_announce.1",
     "choose_monologue": "查他", "speech": "2号是坏人！", "monologue": "实锤了。"},
    {"type": "assassination", "call_id": "5.1.assassinate.2", "assassin_no": 2, "target_no": 1,
     "hit": True, "speech": "就是你！", "monologue": "赌一把。"},
    {"type": "game_end", "winner": "evil", "win_reason": "assassination", "quests": [],
     "seats": [{"no": 1, "persona": "fox", "role": "merlin", "side": "good", "model": "m"}]},
    {"type": "banter", "call_id": "5.1.banter.1", "seat_no": 1, "role": "merlin", "side": "good",
     "text": "唉。", "monologue": "被秒了。"},
]

# 显示名随各局 meta 自带（与 games/validation.py 的 GameSetup.to_meta 快照同源）：
# cast/names 两列平行，host_name 单列；stage 据此自圆，不再读 games 的 cast.yaml。
META_CAST = ["fox", "shy", "diva", "shade", "boss"]
META_NAMES = ["老狐狸", "社恐", "戏精", "阴阳师", "大姐头"]
META_HOST_NAME = "司仪"


def write_replay(root: Path, name: str, events: list[dict] | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    d.joinpath("events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in (events or EVENTS)), encoding="utf-8")
    d.joinpath("meta.json").write_text(json.dumps({"setup": {
        "cast": META_CAST, "names": META_NAMES, "host_cast": "herald", "host_name": META_HOST_NAME,
    }}, ensure_ascii=False), encoding="utf-8")
    return d


@pytest.fixture
def replay_root(tmp_path: Path) -> Path:
    root = tmp_path / "replays"
    root.mkdir()
    write_replay(root, "game1")
    return root


@pytest.fixture
def settings(replay_root: Path) -> StageSettings:
    return StageSettings(
        replay_root=replay_root, stage_token="st4ge", console_token="t0k3n",
        pace=0.0, intermission_seconds=0.0, loop_pool=False,
    )
