"""主持人（host.Host）契约：host 事件穿插各阶段、与玩家线程同源、确定性 call_id、零泄漏。

host 事件 schema：{"type": "host", "beat": <语义标签>, "call_id": <确定性 id>, "text": <台词>}。
- 台词经 Host.announce 同时进全员 inbox 与事件流，玩家看到的公共叙述与 host 事件一字不差；
- call_id = {quest}.{attempt}.host.{beat}（同拍重复补 .2 起序号），同 seed 整局可逐字复现；
- host 只说公开信息：随机后端的私密标记（内心戏话术、湖中仙女真实阵营）绝不出现在台词里；
- 结构化事件（vote_result/quest_result/...）照常 emit，host 事件只是"说出来的话"。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from avalon.run import run_game
from avalon.settings import AvalonSettings
from avalon.state import GameState

_CALL_ID = re.compile(r"^\d+\.\d+\.host\.[a-z_]+(\.\d+)?$")

# 每局必然出现的拍子；lady / assassination 视对局走向（见各断言）。
# 玩家发言（组队宣言/表态/复盘）不是主持拍——主持人只控场串场，绝不复述。
_ALWAYS_BEATS = {"opening", "deal", "propose_intro", "team", "vote_result", "reveal", "closing"}


async def _run_with_events(tmp_path: Path, name: str, **kwargs) -> tuple[GameState, list[dict]]:
    run_dir = tmp_path / name
    final = await run_game(AvalonSettings(player="random", replay=run_dir, **kwargs))
    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    return final, [json.loads(line) for line in lines]


def _thread_text(final: GameState, no: int) -> str:
    t = final.threads[no]
    return "\n".join([m.content for m in t.messages] + list(t.inbox))


async def test_host_events_cover_beats_and_schema(tmp_path) -> None:
    """多种子真跑：每局 host 事件覆盖固定拍子集合、schema 恰为四键、call_id 格式合法且整局唯一；
    assassination 拍当且仅当终局是刺杀，lady 拍次数与令牌传递次数一致。多种子合计两者都被走到。"""
    seen_assassination = seen_lady = False
    for seed in range(8):
        final, events = await _run_with_events(tmp_path, f"g{seed}", players=7, lady=True, seed=seed)
        hosts = [e for e in events if e["type"] == "host"]
        beats = [e["beat"] for e in hosts]
        assert set(beats) >= _ALWAYS_BEATS
        for e in hosts:
            assert set(e) == {"type", "beat", "call_id", "text"}
            assert _CALL_ID.match(e["call_id"]), e["call_id"]
        call_ids = [e["call_id"] for e in hosts]
        assert len(call_ids) == len(set(call_ids)), "host call_id 必须整局唯一"
        assert ("assassination" in beats) == (final.win_reason == "assassination")
        assert beats.count("lady") == len(final.lady_used) - 1
        seen_assassination |= "assassination" in beats
        seen_lady |= "lady" in beats
    assert seen_assassination and seen_lady, "种子集应覆盖刺杀与湖中仙女两条路（否则换种子）"


async def test_host_events_interleave_phases(tmp_path) -> None:
    """host 事件穿插在各阶段之间：开场两拍（opening、deal）按序在一切玩家事件之前——节目单
    是 game_start → host:opening → deal → host:deal（头像先就座再翻牌）；closing 是最后一个
    事件，且每个结构化阶段事件（propose/vote_result/quest_result）都有同拍/配对 host 串场相伴。"""
    _, events = await _run_with_events(tmp_path, "order", players=5, seed=4)
    types = [e["type"] for e in events]
    host_beats = [e["beat"] for e in events if e["type"] == "host"]
    # 开场前两拍 host 按序是 opening、deal（中间夹着 deal 游戏事件——头像先就座再翻牌）
    assert host_beats[0] == "opening" and host_beats[1] == "deal"
    # 第一个 host 之前只可能有 game_start / deal 游戏事件
    first_host = types.index("host")
    assert all(t in {"game_start", "deal"} for t in types[:first_host])
    assert events[-1]["type"] == "host" and events[-1]["beat"] == "closing"
    structural = {"propose": "propose_intro", "propose ": "team", "vote_result": "vote_result", "quest_result": "quest_result"}
    beats = [e["beat"] for e in events if e["type"] == "host"]
    for ev_type, beat in structural.items():
        assert types.count(ev_type.strip()) == beats.count(beat), f"{ev_type.strip()} 事件数应与 host {beat} 拍数一致"


async def test_host_never_quotes_player_speech(tmp_path) -> None:
    """主持人不复述：host 台词（会逐句 TTS）里绝不出现玩家发言的引号原文与不可念的标记符号。"""
    _, events = await _run_with_events(tmp_path, "no-quote", players=7, lady=True, seed=3)
    speeches = [e.get("speech") or e.get("text") for e in events if e["type"] in {"propose", "discuss", "banter", "lady", "assassination"}]
    for e in (e for e in events if e["type"] == "host"):
        for banned in ("「", "」", "【", "】", "✔", "✘", "×"):
            assert banned not in e["text"], f"host 台词含 {banned!r}：{e['text']}"
        for speech in speeches:
            assert speech not in e["text"], f"host 复述了玩家发言：{speech}"


async def test_host_text_identical_to_thread_broadcast(tmp_path) -> None:
    """同源铁律：每条 host 台词一字不差地出现在每个席位的线程里（公共叙述 = 主持人的话）。"""
    final, events = await _run_with_events(tmp_path, "same-source", players=7, lady=True, seed=3)
    host_texts = [e["text"] for e in events if e["type"] == "host"]
    assert host_texts
    for no in final.threads:
        thread = _thread_text(final, no)
        for text in host_texts:
            assert text in thread, f"host 台词未出现在 {no}号 线程：{text}"


async def test_host_speaks_only_public_information(tmp_path) -> None:
    """零泄漏：host 台词不携带任何私密通道内容——随机后端的内心戏话术、任务暗牌的当事人、
    湖中仙女私下看到的真实阵营话术，一概不出现。"""
    _, events = await _run_with_events(tmp_path, "leak", players=7, lady=True, seed=3)
    for e in (e for e in events if e["type"] == "host"):
        for secret in ("牌已扣下", "抛硬币定的", "闭眼点的将", "真实阵营是"):
            assert secret not in e["text"]


async def test_host_call_ids_reproducible(tmp_path) -> None:
    """同 seed 两局：host 事件的 (beat, call_id, text) 序列逐字相同（离线 TTS 对齐的根基）。"""
    _, a = await _run_with_events(tmp_path, "a", players=5, seed=7)
    _, b = await _run_with_events(tmp_path, "b", players=5, seed=7)
    key = lambda evs: [(e["beat"], e["call_id"], e["text"]) for e in evs if e["type"] == "host"]  # noqa: E731
    assert key(a) == key(b)
