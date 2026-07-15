"""Avalon 专用的零网络 RandomActor。"""

from __future__ import annotations

import random
from typing import Any

from pydantic import BaseModel

from avalon.state import Assassination, LadyChoice, Proposal, QuestCard, Side, Statement, TeamVote
from core.sr.loop import Actor


class AvalonRandomActor(Actor):
    """本地随机 agent：按 view 现编合法 JSON 决策。"""

    def __init__(self, rng: random.Random) -> None:
        self._salt = rng.getrandbits(64)

    async def speak(self, seat: int, view: Any, turn_id: str) -> tuple[str, dict[str, Any] | None]:
        phase = getattr(view, "phase", "")
        decision = self._decide(phase, view)
        raw = decision.model_dump_json()
        audit = {
            "call_id": turn_id,
            "seat_no": seat,
            "model": "random",
            "output": raw,
            "ok": True,
        }
        return raw, audit

    async def close(self) -> None: ...

    def _decide(self, phase: str, view: Any) -> BaseModel:
        base = phase.removesuffix(".extract") if hasattr(phase, "removesuffix") else phase
        r = random.Random(f"{self._salt}:{view.seat_no}:{view.quest_no}:{view.attempt}:{base}")

        if base == "propose":
            team = sorted(r.sample(range(1, view.players + 1), view.team_size))
            return Proposal(
                team=team,
                speech=f"（随机）我看 {'、'.join(f'{no}号' for no in team)} 就挺合适，出发。",
                monologue="（随机）闭眼点的将。",
            )
        if base == "discuss":
            return Statement(
                speech="（随机）这队我说不上好坏，先看看大家的票。",
                monologue="（随机）水一句，别看我。",
            )
        if base == "team_vote":
            return TeamVote(approve=r.random() < 0.5, monologue="（随机）抛硬币定的。")
        if base == "quest":
            success = True if view.role.side is Side.GOOD else r.random() < 0.5
            return QuestCard(success=success, monologue="（随机）牌已扣下，面无表情。")
        if base == "assassinate":
            target = r.choice(view.good_suspects())
            return Assassination(
                target_no=target,
                speech=f"（随机）直觉告诉我，{target}号就是梅林。",
                monologue="（随机）蒙的，但要蒙得自信。",
            )
        if base == "lady_choose":
            return LadyChoice(target_no=r.choice(view.lady_candidates()), monologue="（随机）就查你了。")
        if base == "lady_announce":
            claim = view.lady_seen if r.random() < 0.5 else ("evil" if view.lady_seen == "good" else "good")
            side = "蓝方" if claim == "good" else "红方"
            return Statement(
                speech=f"（随机）我查验了{view.lady_target}号——{side}。",
                monologue="（随机）信不信由你们。",
            )
        if base == "banter":
            return Statement(
                speech="（随机）这局打完了，各位辛苦，下把再战。",
                monologue="（随机）复盘就免了，赢牌全靠脸。",
            )
        raise ValueError(f"未知决策阶段：{base}")
