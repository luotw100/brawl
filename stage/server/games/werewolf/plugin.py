"""狼人杀插件：把 replay 事件翻成舞台指令、折进 WerewolfScene，并交付进度摘要。"""

from __future__ import annotations

from typing import Any

from server import replay
from server.games.base import AdaptContext
from server.games.werewolf.protocol import (
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
from server.protocol import BaseScene, Directive, FeedItem, GameResult, HostSpeech, Intermission

_PHASE = {
    "discussion": "白天发言",
    "last_words": "遗言",
    "hunter": "猎人开枪",
    "banter": "赛后复盘",
}


class WerewolfPlugin:
    """GamePlugin 实现：狼人杀。无状态，所有随局数据经 AdaptContext / 入参传入。"""

    id = "werewolf"

    def new_scene(self) -> WerewolfScene:
        return WerewolfScene()

    def adapt(self, event: dict[str, Any], ctx: AdaptContext) -> Directive | None:
        t = event["type"]
        if t == "host":
            if event["beat"] == "reveal":
                return None
            return HostSpeech(
                name=ctx.host_name, beat=event["beat"], text=event["text"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "game_start":
            return GameOpen(
                board_name=event["board_name"], players=event["players"],
                wolves=event["wolves"], gods=event["gods"], villagers=event["villagers"],
                seats=[self._seat(s, ctx) for s in event.get("seats", [])],
            )
        if t == "deal":
            return Deal(seats=[self._seat(s, ctx) for s in event["seats"]])
        if t == "night_start":
            return NightStart(day=event["day"])
        if t == "wolf_kill":
            return WolfKill(
                day=event["day"], target_no=event["target_no"],
                votes=[WolfVote(
                    seat_no=v["seat_no"], target_no=v["target_no"],
                    speech=v["speech"], monologue=v["monologue"],
                ) for v in event["votes"]],
            )
        if t == "seer_check":
            return SeerCheck(
                day=event["day"], seat_no=event["seat_no"], target_no=event["target_no"],
                result=event["result"], monologue=event["monologue"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "witch_action":
            return WitchAction(
                day=event["day"], seat_no=event["seat_no"], save=event["save"],
                poison_target_no=event["poison_target_no"], monologue=event["monologue"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "death_resolved":
            return DeathResolved(day=event["day"], deaths=[DeathInfo(**d) for d in event["deaths"]])
        if t == "role_revealed":
            return RoleReveal(day=event["day"], seat_no=event["seat_no"], role=event["role"], reason=event["reason"])
        if t == "idiot_revealed":
            return IdiotReveal(day=event["day"], seat_no=event["seat_no"])
        if t == "speech":
            return Speech(
                day=event["day"], phase=event["phase"], seat_no=event["seat_no"],
                text=event["text"], monologue=event["monologue"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "vote_result":
            return VoteReveal(
                day=event["day"], exiled=event["exiled"], tied=event["tied"],
                votes=[ExileVote(seat_no=v["seat_no"], target_no=v["target_no"], monologue=v["monologue"]) for v in event["votes"]],
            )
        if t == "hunter_shot":
            return HunterShot(
                day=event["day"], hunter_no=event["hunter_no"], target_no=event["target_no"],
                text=event["speech"], monologue=event["monologue"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "game_end":
            return GameOver(
                winner=event["winner"], win_reason=event["win_reason"], day=event["day"],
                seats=[self._seat(s, ctx) for s in event["seats"]],
                deaths=[DeathInfo(**d) for d in event["deaths"]],
            )
        if t == "banter":
            return Speech(
                phase="banter", seat_no=event["seat_no"], text=event["text"], monologue=event["monologue"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        raise ValueError(f"未知事件类型：{t}（events 契约变了，adapter 要跟上）")

    def _seat(self, s: dict[str, Any], ctx: AdaptContext) -> SeatInfo:
        return SeatInfo(
            no=s["no"], name=ctx.names.get(s["persona"], s["persona"]), persona=s["persona"],
            role=s["role"], side=s["side"], model=s.get("model", ""),
            alive=s.get("alive", True), revealed=s.get("revealed", False), can_vote=s.get("can_vote", True),
            avatar_url=ctx.avatars.get(s["no"]),
            model_display=ctx.model_labels.get(s.get("model") or ""),
        )

    def apply(self, scene: WerewolfScene, item: FeedItem) -> None:
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
                    self._mark_dead(scene, death)
            case RoleReveal():
                self._mark_dead(scene, DeathInfo(
                    day=directive.day, seat_no=directive.seat_no, role=directive.role, reason=directive.reason,
                ))
                scene.timeline.append(DeathLog(day=directive.day, seat_no=directive.seat_no, role=directive.role, reason=directive.reason))
            case IdiotReveal():
                self._replace_seat(scene, directive.seat_no, revealed=True, can_vote=False)
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
            case Intermission():
                pass

    def _replace_seat(self, scene: WerewolfScene, seat_no: int, **updates: Any) -> None:
        scene.seats = [s.model_copy(update=updates) if s.no == seat_no else s for s in scene.seats]

    def _mark_dead(self, scene: WerewolfScene, death: DeathInfo) -> None:
        if death.seat_no not in {d.seat_no for d in scene.deaths}:
            scene.deaths.append(death)
        self._replace_seat(scene, death.seat_no, alive=False, revealed=True, role=death.role)

    def is_opening_anchor(self, directive: Directive) -> bool:
        return isinstance(directive, Deal)

    def result(self, directive: Directive) -> GameResult | None:
        if isinstance(directive, GameOver):
            return GameResult(winner=directive.winner, win_reason=directive.win_reason)
        return None

    def summary(self, directive: Directive, scene: BaseScene) -> tuple[str, str, str]:
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
            case Intermission():
                return "幕间", "dim", ""
            case _:
                return "?", "white", type(directive).__name__


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
