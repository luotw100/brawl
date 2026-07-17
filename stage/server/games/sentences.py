"""Sentence Runtime 句子流 → 旧 events 契约的转换桥（阶段 6 过渡层）。

新 replay（games 的 sentence runtime）只落 sentences.jsonl：一行一句（玩家发言 + host
串场 + audience 事件同一条总线），action 里是玩法 rules 的结构化 payload。Stage 的各
玩法 plugin 仍消费旧 events.jsonl 契约——本模块按玩法把句子流翻译成那套事件形状，
让 plugin/engine/录播不必知道底下换了运行时。旧 replay 自带 events.jsonl 时优先生效，
本桥只在 events.jsonl 缺席时接管（见 replay.load_events）。

call_id 约定：新 TTS（games/core/sr/tts.py）的 manifest 以 `{seq}.{speaker}` 为键，
本桥产出事件的 call_id 与之同构（host 事件为 `{seq}.host`），音频 join 方能命中。
聚合型事件（票型/任务结果/狼刀）的子项各自带原句子的 call_id——逐条配音逐条对得上。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_sentences(replay_dir: Path) -> list[dict[str, Any]]:
    path = replay_dir / "sentences.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def events_from_sentences(replay_dir: Path, game: str) -> list[dict[str, Any]]:
    """一句句读出 sentences.jsonl 并翻译成旧 events 契约。game 与 meta.game 同源。"""
    sentences = load_sentences(replay_dir)
    meta = _meta(replay_dir)
    if game == "avalon":
        return _avalon_events(sentences, meta)
    if game == "werewolf":
        return _werewolf_events(sentences, meta)
    raise ValueError(f"sentences 桥未登记的玩法：{game}")


def _meta(replay_dir: Path) -> dict[str, Any]:
    path = replay_dir / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _action(sent: dict[str, Any]) -> dict[str, Any]:
    a = sent.get("action") or ""
    return json.loads(a) if a else {}


def _text(sent: dict[str, Any]) -> str:
    return "\n".join(d.get("text", "") for d in sent.get("deliveries") or [] if d.get("text"))


def _call_id(sent: dict[str, Any]) -> str:
    return f"{sent['seq']}.{sent['speaker']}"


# ---------------------------------------------------------------- Avalon
#
# turn_id 形状：q{quest}.a{attempt}.<phase>[.<idx>] / assassinate / banter.{i} /
# pregame.opening / postgame.*。action.kind 见 games/avalon/rules_engine.py。

_AV_TURN = re.compile(r"q(\d+)\.a(\d+)\.(\w+)(?:\.(\d+))?$")


def _avalon_events(sentences: list[dict[str, Any]], meta: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    setup = meta.get("setup") or {}
    seats_by_no: dict[int, dict[str, Any]] = {}
    pending_votes: list[dict[str, Any]] = []  # team_vote 句攒组，等 host 的 vote_result _payload
    pending_cards: list[dict[str, Any]] = []
    lady_choose: dict[str, Any] | None = None
    succeeded = failed = 0

    def _qa(turn_id: str) -> tuple[int, int]:
        m = _AV_TURN.match(turn_id)
        assert m, f"avalon turn_id 形状漂移：{turn_id}"
        return int(m.group(1)), int(m.group(2))

    for sent in sentences:
        a = _action(sent)
        kind = a.get("kind")
        tid = sent["turn_id"]
        speaker = sent["speaker"]

        if kind == "game_start":
            seats = a.get("seats", [])
            seats_by_no = {s["no"]: s for s in seats}
            events.append({
                "type": "game_start", "players": a["players"], "good": a["good"], "evil": a["evil"],
                "seats": seats,
            })
            events.append({"type": "host", "beat": "opening", "call_id": _call_id(sent), "text": _text(sent)})
            events.append({
                "type": "deal",
                "seats": seats,
                "leader_no": setup.get("first_leader"),
                "lady_holder": setup.get("lady_holder"),
            })
            continue

        if speaker == "host":
            beat = {
                "team_intro": "team", "vote_result": "vote_result", "quest_result": "quest_result",
                "lady_transfer": "lady", "assassination_result": "assassination",
                "reveal": "reveal", "closing": "closing",
            }.get(kind)
            if kind == "vote_result":
                q, att = _qa(tid)
                events.append({
                    "type": "vote_result", "quest": q, "attempt": att,
                    "votes": pending_votes, "approved": a["approved"], "rejections": a["rejections"],
                })
                pending_votes = []
            elif kind == "quest_result":
                q, att = _qa(tid)
                if a["result"] == "success":
                    succeeded += 1
                else:
                    failed += 1
                events.append({
                    "type": "quest_result", "quest": q, "team": [c["seat_no"] for c in pending_cards],
                    "cards": pending_cards, "fails": a["fails"], "fails_required": a["fails_required"],
                    "result": a["result"], "succeeded": succeeded, "failed": failed,
                })
                pending_cards = []
            elif kind == "reveal":
                events.append({
                    "type": "game_end", "winner": a["winner"], "win_reason": a["win_reason"], "quests": [],
                    "seats": [
                        {**s, "alive": True}
                        for s in sorted(seats_by_no.values(), key=lambda s: s["no"])
                    ],
                })
            if beat is not None:
                events.append({"type": "host", "beat": beat, "call_id": _call_id(sent), "text": _text(sent)})
            continue

        # 玩家句子
        if kind == "propose":
            q, att = _qa(tid)
            events.append({
                "type": "propose", "quest": q, "attempt": att, "call_id": _call_id(sent),
                "leader_no": speaker, "team": a["team"], "speech": a["speech"], "monologue": a["monologue"],
            })
        elif kind == "discuss":
            q, att = _qa(tid)
            events.append({
                "type": "discuss", "quest": q, "attempt": att, "call_id": _call_id(sent),
                "seat_no": speaker, "text": a["speech"], "monologue": a["monologue"],
            })
        elif kind == "team_vote":
            pending_votes.append({
                "seat_no": speaker, "approve": a["approve"], "monologue": a["monologue"],
                "call_id": _call_id(sent),
            })
        elif kind == "quest":
            pending_cards.append({
                "seat_no": speaker, "success": a["success"], "monologue": a["monologue"],
                "call_id": _call_id(sent),
            })
        elif kind == "lady_choose":
            lady_choose = {**a, "call_id": _call_id(sent)}
        elif kind == "lady_announce":
            choose = lady_choose or {}
            events.append({
                "type": "lady", "holder_no": speaker, "target_no": choose.get("target_no"),
                "choose_monologue": choose.get("monologue", ""), "truth": a.get("truth"),
                "speech": a["speech"], "monologue": a["monologue"],
                "choose_call_id": choose.get("call_id"), "announce_call_id": _call_id(sent),
            })
            lady_choose = None
        elif kind == "assassinate":
            events.append({
                "type": "assassination", "call_id": _call_id(sent), "assassin_no": speaker,
                "target_no": a["target_no"], "hit": a["hit"], "speech": a["speech"], "monologue": a["monologue"],
            })
        elif kind == "banter":
            seat = seats_by_no.get(speaker, {})
            events.append({
                "type": "banter", "call_id": _call_id(sent), "seat_no": speaker,
                "role": seat.get("role", ""), "side": seat.get("side", ""),
                "text": a["speech"], "monologue": a["monologue"],
            })
        else:
            raise ValueError(f"avalon sentences 桥遇到未知 action kind：{kind}")
    return events


# ---------------------------------------------------------------- Werewolf
#
# turn_id 形状：day{d}.<phase>[.<idx>] / banter.{i} / pregame.opening / postgame.*。
# action.kind 见 games/werewolf/rules_engine.py。

_WW_TURN = re.compile(r"day(\d+)\.(\w+)(?:\.(\d+))?$")


def _werewolf_events(sentences: list[dict[str, Any]], meta: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seats_by_no: dict[int, dict[str, Any]] = {}
    pending_wolf: list[dict[str, Any]] = []  # 当夜狼刀提议攒组
    pending_votes: list[dict[str, Any]] = []
    day = 1
    deaths: list[dict[str, Any]] = []

    def _day_of(tid: str) -> int:
        m = _WW_TURN.match(tid)
        return int(m.group(1)) if m else day

    def _flush_wolf(nxt: dict[str, Any] | None) -> None:
        # 当夜狼刀提议攒齐（下一句不再是同夜狼刀）→ 出聚合 wolf_kill 事件（多数决，平取最小号）。
        nonlocal pending_wolf
        if not pending_wolf:
            return
        nxt_kind = _action(nxt).get("kind") if nxt else None
        nxt_day = _day_of(nxt["turn_id"]) if nxt else None
        if nxt_kind == "wolf_kill" and nxt_day == day:
            return
        from collections import Counter

        counts = Counter(v["target_no"] for v in pending_wolf)
        target = min((-n, no) for no, n in counts.items())[1]
        events.append({"type": "wolf_kill", "day": day, "target_no": target, "votes": pending_wolf})
        pending_wolf = []

    for idx, sent in enumerate(sentences):
        nxt = sentences[idx + 1] if idx + 1 < len(sentences) else None
        a = _action(sent)
        kind = a.get("kind")
        tid = sent["turn_id"]
        speaker = sent["speaker"]

        if kind == "game_start":
            seats = a.get("seats", [])
            seats_by_no = {s["no"]: s for s in seats}
            events.append({
                "type": "game_start", "board_name": a.get("board_name", ""), "players": a["players"],
                "wolves": a["wolves"], "gods": a["gods"], "villagers": a["villagers"], "seats": seats,
            })
            events.append({"type": "host", "beat": "opening", "call_id": _call_id(sent), "text": _text(sent)})
            events.append({"type": "deal", "seats": seats})
            continue
        if kind == "night_start":
            day = a["day"]
            events.append({"type": "night_start", "day": day})
            continue

        if speaker == "host":
            if kind == "dawn":
                day = a.get("day", day)
                dths = a.get("deaths", [])
                deaths.extend(dths)
                events.append({"type": "death_resolved", "day": day, "deaths": dths})
                for d in dths:
                    events.append({
                        "type": "role_revealed", "day": day,
                        "seat_no": d["seat_no"], "role": d["role"], "reason": d["reason"],
                    })
                events.append({"type": "host", "beat": "day_start", "call_id": _call_id(sent), "text": _text(sent)})
            elif kind == "vote_result":
                day = _day_of(tid)
                events.append({
                    "type": "vote_result", "day": day, "votes": pending_votes,
                    "exiled": a["exiled"], "tied": a["tied"],
                })
                pending_votes = []
                events.append({"type": "host", "beat": "vote_result", "call_id": _call_id(sent), "text": _text(sent)})
                if a.get("exiled") is not None and not a.get("tied"):
                    ex = a["exiled"]
                    seat = seats_by_no.get(ex, {})
                    if seat.get("role") != "idiot":
                        deaths.append({"seat_no": ex, "role": seat.get("role", ""), "reason": "exile", "day": day})
            elif kind == "idiot_revealed":
                events.append({"type": "idiot_revealed", "day": _day_of(tid), "seat_no": a["seat_no"]})
            elif kind == "idiot_immune":
                pass  # 旧契约没有对应事件（旧引擎当时只广播文本），跳过
            elif kind == "hunter_shot_result":
                if a.get("target_no") is not None and a.get("victim_role"):
                    deaths.append({
                        "seat_no": a["target_no"], "role": a["victim_role"], "reason": "hunter_shot", "day": _day_of(tid),
                    })
                    events.append({
                        "type": "role_revealed", "day": _day_of(tid),
                        "seat_no": a["target_no"], "role": a["victim_role"], "reason": "hunter_shot",
                    })
            elif kind == "reveal":
                seats = []
                dead_nos = {d["seat_no"] for d in deaths}
                for s in sorted(seats_by_no.values(), key=lambda s: s["no"]):
                    seats.append({**s, "alive": s["no"] not in dead_nos})
                events.append({
                    "type": "game_end", "winner": a["winner"], "win_reason": a["win_reason"], "day": day,
                    "seats": seats, "deaths": a.get("deaths", deaths),
                })
            elif kind == "closing":
                events.append({"type": "host", "beat": "closing", "call_id": _call_id(sent), "text": _text(sent)})
            else:
                raise ValueError(f"werewolf sentences 桥遇到未知 host kind：{kind}")
            continue

        # 玩家句子
        if kind == "wolf_kill":
            day = _day_of(tid)
            pending_wolf.append({
                "seat_no": speaker, "target_no": a["target_no"], "speech": a["speech"],
                "monologue": a["monologue"], "call_id": _call_id(sent),
            })
            _flush_wolf(nxt)  # 当夜最后一只狼提议后即出聚合事件（旧契约里它在天亮之前）
        elif kind == "seer_check":
            events.append({
                "type": "seer_check", "day": _day_of(tid), "seat_no": speaker,
                "target_no": a["target_no"], "result": a["result"], "monologue": a["monologue"],
                "call_id": _call_id(sent),
            })
        elif kind == "witch_action":
            events.append({
                "type": "witch_action", "day": _day_of(tid), "seat_no": speaker,
                "save": a["save"], "poison_target_no": a["poison_target_no"], "monologue": a["monologue"],
                "call_id": _call_id(sent),
            })
        elif kind in ("last_words", "discuss"):
            events.append({
                "type": "speech", "day": _day_of(tid),
                "phase": "last_words" if kind == "last_words" else "discussion",
                "seat_no": speaker, "text": a["speech"], "monologue": a["monologue"],
                "call_id": _call_id(sent),
            })
        elif kind == "vote":
            pending_votes.append({
                "seat_no": speaker, "target_no": a["target_no"], "monologue": a["monologue"],
                "call_id": _call_id(sent),
            })
        elif kind == "hunter_shot":
            events.append({
                "type": "hunter_shot", "day": _day_of(tid), "hunter_no": speaker,
                "target_no": a["target_no"], "speech": a["speech"], "monologue": a["monologue"],
                "call_id": _call_id(sent),
            })
        elif kind == "banter":
            seat = seats_by_no.get(speaker, {})
            events.append({
                "type": "banter", "call_id": _call_id(sent), "seat_no": speaker,
                "role": seat.get("role", ""), "side": seat.get("side", ""),
                "text": a["speech"], "monologue": a["monologue"],
            })
        else:
            raise ValueError(f"werewolf sentences 桥遇到未知 action kind：{kind}")
    return events
