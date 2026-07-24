"""狼人杀演出投影：把存档重放成舞台指令流，并交付展示态 reducer 与录制挂钩。

这是"狼人杀怎么上演"的唯一落脚点。投影纪律：

- 一切规则事实（狼刀结果/查验阵营/放逐票型/胜负）只取自 action payload——它们是
  rules 在生成时冻结的 god-view 真相。这里绝不重新计票、绝不反解 turn_id。
- 少量展示上下文（天数、当夜狼刀提议、当轮放逐票独白）随流累积，是纯显示组装：
  独白从未进过 rules 账本，只能在这里随流收集。
- 未知 kind 直接抛：投影漂移要立刻暴露，不静默吞。

两处旧桥丢掉的配音在这里接回（plan.yaml 的 lead_in_next 槽位本来就是给它们的）：
- night_start：入夜主持词（旧桥只发视觉事件，音频丢了）
- seer_result：查验结果的主持私语（挂到 SeerCheck 拍上，比沉默的翻牌有戏）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from brawl.games.werewolf.protocol import (
    Deal,
    DeathInfo,
    DeathLog,
    DeathResolved,
    ExileVote,
    GameOpen,
    GameOver,
    HunterShot,
    IdiotReveal,
    NightStart,
    RoleReveal,
    SeatInfo,
    SeerCheck,
    Speech,
    VoteLog,
    VoteReveal,
    WerewolfScene,
    WitchAction,
    WolfKill,
    WolfVote,
)
from brawl.games.werewolf.validation import ReplayMeta
from brawl.runtime.types import Sentence
from brawl.stage.protocol import BaseScene, Directive, FeedItem, HostSpeech
from brawl.stage.stagecast import MediaResolver, StageGame

_PHASE = {
    "discussion": "白天发言",
    "last_words": "遗言",
    "hunter": "猎人开枪",
    "banter": "赛后复盘",
}


def stagecast(sentences: list[Sentence], meta: dict[str, Any], media: MediaResolver) -> list[Directive]:
    """存档 → 舞台指令流（纯投影）。"""
    projector = _Projector(ReplayMeta.model_validate_json(json.dumps(meta), strict=True), media)
    out: list[Directive] = []
    for sent in sentences:
        out.extend(projector.feed(sent))
    return out


class _Projector:
    """逐句投影器：buf_* 是当轮/当夜展示缓存，ctx_day 是显示天数。"""

    def __init__(self, meta: ReplayMeta, media: MediaResolver) -> None:
        self._meta = meta
        self._media = media
        self._seats: dict[int, SeatInfo] = {}
        self._day = 0
        self._buf_wolves: list[dict[str, Any]] = []  # 当夜各狼提议（seat/target/speech/monologue）
        self._buf_votes: dict[int, tuple[int, str]] = {}  # seat → (target_no, monologue)
        self._buf_seer: tuple[int, int, str] | None = None  # (seat, target_no, monologue)
        self._buf_poison: int | None = None  # 当夜毒药目标（dawn 死因推导用）
        self._idiots: set[int] = set()  # 已翻牌白痴（终局座位表 can_vote 用）

    # ---------------------------------------------------------------- 主分派

    def feed(self, sent: Sentence) -> list[Directive]:
        action = json.loads(sent.action) if sent.action else {}
        kind = action.get("kind")
        if sent.speaker == "host":
            return self._host(sent, action, kind)
        return self._player(sent, action, kind)

    def _player(self, sent: Sentence, a: dict[str, Any], kind: str | None) -> list[Directive]:
        seat = sent.speaker
        assert isinstance(seat, int)
        call_id = _call_id(sent)
        if kind == "wolf_kill":
            self._buf_wolves.append({
                "seat_no": seat, "target_no": a["target_no"],
                "speech": a["speech"], "monologue": a["monologue"],
            })
            return []
        if kind == "seer_check":
            self._buf_seer = (seat, a["target_no"], a["monologue"])
            return []
        if kind == "witch_action":
            self._buf_poison = a["poison_target_no"]
            return [
                WitchAction(
                    day=self._day, seat_no=seat, save=a["save"],
                    poison_target_no=a["poison_target_no"], monologue=a["monologue"],
                )
            ]
        if kind in ("last_words", "discuss"):
            return [self._speech(sent, "last_words" if kind == "last_words" else "discussion", a)]
        if kind == "vote":
            self._buf_votes[seat] = (a["target_no"], a["monologue"])
            return []
        if kind == "hunter_shot":
            return [
                HunterShot(
                    day=self._day, hunter_no=seat, target_no=a["target_no"],
                    text=a["speech"], monologue=a["monologue"],
                    call_id=call_id, audio_url=self._audio(call_id),
                )
            ]
        if kind == "banter":
            return [self._speech(sent, "banter", a)]
        raise ValueError(f"werewolf 投影遇到未知玩家 kind：{kind}（rules 契约变了，stagecast 要跟上）")

    def _host(self, sent: Sentence, a: dict[str, Any], kind: str | None) -> list[Directive]:
        if kind == "game_start":
            self._seats = {s["no"]: self._seat(s) for s in a["seats"]}
            seats = [self._seats[no] for no in sorted(self._seats)]
            return [
                GameOpen(
                    board_name=a["board_name"], players=a["players"],
                    wolves=a["wolves"], gods=a["gods"], villagers=a["villagers"], seats=seats,
                ),
                self._host_speech(sent, "opening"),
                Deal(seats=seats),
            ]
        if kind == "night_start":
            self._day = a["day"]
            self._buf_poison = None
            return [self._host_speech(sent, "night_start"), NightStart(day=a["day"])]
        if kind == "wolf_kill_result":
            votes = [
                WolfVote(
                    seat_no=v["seat_no"], target_no=v["target_no"],
                    speech=v["speech"], monologue=v["monologue"],
                )
                for v in self._buf_wolves
            ]
            self._buf_wolves = []
            return [WolfKill(day=a["day"], target_no=a["target_no"], votes=votes)]
        if kind == "seer_result":
            assert self._buf_seer is not None, "seer_result 之前缺 seer_check"
            seat, target, monologue = self._buf_seer
            self._buf_seer = None
            # 查验视觉拍的配音用主持私语（玩家 seer_check 句只有内心独白，本就不出声）。
            call_id = _call_id(sent)
            return [
                SeerCheck(
                    day=a["day"], seat_no=seat, target_no=target,
                    result=a["result"], monologue=monologue,
                    call_id=call_id, audio_url=self._audio(call_id),
                )
            ]
        if kind == "witch_result":
            return []  # 裁决明细（spoken=False 的账本事件）；WitchAction 拍已覆盖
        if kind == "dawn":
            self._day = a["day"]
            deaths = [self._death(d) for d in a["deaths"]]
            out: list[Directive] = [DeathResolved(day=self._day, deaths=deaths)]
            out.extend(
                RoleReveal(day=self._day, seat_no=d.seat_no, role=d.role, reason=d.reason)
                for d in deaths
            )
            out.append(self._host_speech(sent, "day_start"))
            return out
        if kind == "vote_result":
            self._day = a["day"]
            votes = [
                ExileVote(seat_no=seat, target_no=target, monologue=monologue)
                for seat, (target, monologue) in sorted(self._buf_votes.items())
            ]
            self._buf_votes = {}
            return [
                VoteReveal(day=self._day, votes=votes, exiled=a["exiled"], tied=a["tied"]),
                self._host_speech(sent, "vote_result"),
            ]
        if kind == "idiot_revealed":
            self._idiots.add(a["seat_no"])
            return [IdiotReveal(day=a["day"], seat_no=a["seat_no"])]
        if kind == "idiot_immune":
            return []  # 与 idiot_revealed 同信息（翻牌免死），IdiotReveal 拍已覆盖
        if kind == "hunter_shot_result":
            out = [self._host_speech(sent, "hunter_result")]
            if a.get("target_no") is not None and a.get("victim_role"):
                out.append(
                    RoleReveal(
                        day=self._day, seat_no=a["target_no"],
                        role=a["victim_role"], reason="hunter_shot",
                    )
                )
            return out
        if kind == "reveal":
            deaths = [DeathInfo(**d) for d in a["deaths"]]
            dead_nos = {d.seat_no for d in deaths}
            seats = []
            for no in sorted(self._seats):
                seat = self._seats[no]
                if no in dead_nos:
                    seat = seat.model_copy(update={"alive": False, "revealed": True})
                elif no in self._idiots:
                    seat = seat.model_copy(update={"revealed": True, "can_vote": False})
                seats.append(seat)
            return [
                GameOver(
                    winner=a["winner"], win_reason=a["win_reason"], day=self._day,
                    seats=seats, deaths=deaths,
                )
            ]
        if kind == "closing":
            return [self._host_speech(sent, "closing")]
        raise ValueError(f"werewolf 投影遇到未知 host kind：{kind}（rules 契约变了，stagecast 要跟上）")

    # ---------------------------------------------------------------- 组装件

    def _seat(self, s: dict[str, Any]) -> SeatInfo:
        setup = self._meta.setup
        no = s["no"]
        return SeatInfo(
            no=no, name=setup.names[no - 1], persona=s["persona"],
            role=s["role"], side=s["side"], model=s.get("model", ""),
            avatar_url=self._media.avatar(no),
            model_display=self._meta.model_labels.get(s.get("model") or ""),
        )

    def _death(self, d: dict[str, Any]) -> DeathInfo:
        """dawn payload 不带死因（契约冻结）：毒药目标当夜缓存可查，其余即狼刀。"""
        reason = "poison" if d["seat_no"] == self._buf_poison else "wolf_kill"
        return DeathInfo(seat_no=d["seat_no"], role=d["role"], reason=reason, day=d["day"])

    def _speech(self, sent: Sentence, phase: str, a: dict[str, Any]) -> Speech:
        call_id = _call_id(sent)
        seat = sent.speaker
        assert isinstance(seat, int)
        day = None if phase == "banter" else self._day
        return Speech(
            day=day, phase=phase, seat_no=seat, text=a["speech"], monologue=a["monologue"],
            call_id=call_id, audio_url=self._audio(call_id),
        )

    def _host_speech(self, sent: Sentence, beat: str) -> HostSpeech:
        call_id = _call_id(sent)
        return HostSpeech(
            name=self._meta.setup.host_name, beat=beat, text=_spoken_text(sent),
            call_id=call_id, audio_url=self._audio(call_id),
        )

    def _audio(self, call_id: str) -> str | None:
        resolved = self._media.audio(call_id)
        return resolved[0] if resolved else None


# ---------------------------------------------------------------- reducer / 录制挂钩（展示态折叠）


def new_scene() -> WerewolfScene:
    return WerewolfScene()


def apply(scene: BaseScene, item: FeedItem) -> None:
    """把一拍折进狼人杀状态（feed 入队与窗口护栏由引擎统一做，这里只折玩法态）。"""
    assert isinstance(scene, WerewolfScene)
    directive = item.directive
    scene.speaking = None

    match directive:
        case GameOpen():
            scene.board_name = directive.board_name
            scene.seats = directive.seats
            scene.dealt = False
            scene.day = 0
            scene.phase = "opening"
            scene.night_target = None
            scene.last_exiled = None
            scene.winner = None
            scene.win_reason = None
            scene.deaths = []
            scene.timeline = []
        case Deal():
            scene.seats = directive.seats
            scene.dealt = True
        case NightStart():
            scene.day = directive.day
            scene.phase = "night"
            scene.night_target = None
            scene.last_exiled = None
        case WolfKill():
            scene.day = directive.day
            scene.phase = "night"
            scene.night_target = directive.target_no
        case SeerCheck():
            scene.speaking = directive.seat_no
        case WitchAction():
            scene.speaking = directive.seat_no
        case DeathResolved():
            scene.day = directive.day
            for death in directive.deaths:
                _mark_dead(scene, death)
        case RoleReveal():
            death = DeathInfo(day=directive.day, seat_no=directive.seat_no, role=directive.role, reason=directive.reason)
            _mark_dead(scene, death)
            scene.timeline.append(DeathLog(day=directive.day, seat_no=directive.seat_no, role=directive.role, reason=directive.reason))
        case IdiotReveal():
            _replace_seat(scene, directive.seat_no, revealed=True, can_vote=False)
        case Speech():
            scene.day = directive.day or scene.day
            scene.phase = "postgame" if directive.phase == "banter" else "day"
            scene.speaking = directive.seat_no
        case VoteReveal():
            scene.day = directive.day
            scene.phase = "day"
            scene.last_exiled = directive.exiled
            scene.timeline.append(VoteLog(day=directive.day, exiled=directive.exiled, tied=directive.tied, votes=directive.votes))
        case HunterShot():
            scene.day = directive.day
            scene.phase = "day"
            scene.speaking = directive.hunter_no
        case GameOver():
            scene.day = directive.day
            scene.phase = "postgame"
            scene.seats = directive.seats
            scene.deaths = directive.deaths
            scene.winner = directive.winner
            scene.win_reason = directive.win_reason
        case HostSpeech():
            pass


def is_opening_anchor(directive: Directive) -> bool:
    return isinstance(directive, Deal)


def terminal(directive: Directive) -> bool:
    return isinstance(directive, GameOver)


def summary(directive: Directive, scene: BaseScene) -> tuple[str, str, str]:
    """一拍 → (标签, 颜色, 详情)，给录制进度条打成一行。"""
    seats = getattr(scene, "seats", [])
    match directive:
        case GameOpen():
            return "开局", "cyan", f"{directive.players} 人 · 狼 {directive.wolves} / 神 {directive.gods} / 民 {directive.villagers}"
        case Deal():
            return "发牌", "cyan", f"{len(directive.seats)} 人"
        case HostSpeech():
            return "主持", "yellow", f"{directive.name}：{_snip(directive.text)}"
        case NightStart():
            return "入夜", "blue", f"第 {directive.day} 天"
        case WolfKill():
            return "狼刀", "red", f"刀口 {directive.target_no} 号"
        case SeerCheck():
            return "查验", "magenta", f"{_seat_name(seats, directive.seat_no)} 查 {directive.target_no}号"
        case WitchAction():
            return "女巫", "magenta", _witch_text(directive)
        case DeathResolved():
            return "死讯", "red", "平安夜" if not directive.deaths else "、".join(f"{d.seat_no}号" for d in directive.deaths)
        case RoleReveal():
            return "翻牌", "red", f"{directive.seat_no}号 {directive.role}"
        case IdiotReveal():
            return "白痴", "yellow", f"{directive.seat_no}号翻牌免死"
        case Speech():
            return _PHASE.get(directive.phase, directive.phase), "green", f"{_seat_name(seats, directive.seat_no)}：{_snip(directive.text)}"
        case VoteReveal():
            return "投票", "magenta", "平票" if directive.tied else f"放逐 {directive.exiled}号"
        case HunterShot():
            return "猎枪", "red", "不开枪" if directive.target_no is None else f"带走 {directive.target_no}号"
        case GameOver():
            return "终局", "bold red", f"{directive.winner} 胜 · {directive.win_reason}"
        case _:
            return "?", "white", type(directive).__name__


STAGE_GAME = StageGame(
    id="werewolf",
    title="狼人杀",
    stagecast=stagecast,
    new_scene=new_scene,
    apply=apply,
    summary=summary,
    is_opening_anchor=is_opening_anchor,
    terminal=terminal,
    plan_path=Path(__file__).parent / "plan.yaml",
)


# ---------------------------------------------------------------- 小工具


def _replace_seat(scene: WerewolfScene, seat_no: int, **updates: Any) -> None:
    scene.seats = [s.model_copy(update=updates) if s.no == seat_no else s for s in scene.seats]


def _mark_dead(scene: WerewolfScene, death: DeathInfo) -> None:
    if death.seat_no not in {d.seat_no for d in scene.deaths}:
        scene.deaths.append(death)
    _replace_seat(scene, death.seat_no, alive=False, revealed=True, role=death.role)


def _call_id(sent: Sentence) -> str:
    return f"{sent.seq}.{sent.speaker}"


def _spoken_text(sent: Sentence) -> str:
    """host 句子的口播文本：只取 spoken=True 的 delivery（账本类 spoken=False 不进旁白）。"""
    return "\n".join(d.text for d in sent.deliveries if d.spoken and d.text)


def _witch_text(directive: WitchAction) -> str:
    parts: list[str] = []
    if directive.save:
        parts.append("解药")
    if directive.poison_target_no is not None:
        parts.append(f"毒 {directive.poison_target_no}号")
    return "、".join(parts) if parts else "未用药"


def _snip(text: str, n: int = 36) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _seat_name(seats: list[SeatInfo], seat_no: int) -> str:
    return next((s.name for s in seats if s.no == seat_no), f"{seat_no}号")
