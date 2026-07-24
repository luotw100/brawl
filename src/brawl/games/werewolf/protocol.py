"""狼人杀的舞台协议：指令形状 + Scene 字段。全部子类化框架信封（brawl.stage.protocol）。

这是上帝视角狼人杀：身份开局亮给观众，夜间行动、白天发言、放逐、翻牌、终局都由
reducer 折进一份 WerewolfScene。框架引擎只认 brawl.stage.protocol 的基类。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SerializeAsAny

from brawl.stage.protocol import BaseScene, Directive, TimelineEntry


class SeatInfo(BaseModel):
    no: int
    name: str
    persona: str
    role: str
    side: Literal["good", "werewolf"]
    model: str
    alive: bool = True
    revealed: bool = False
    can_vote: bool = True
    avatar_url: str | None = None
    model_display: str | None = None


class GameOpen(Directive):
    kind: Literal["ww_game_open"] = "ww_game_open"
    board_name: str
    players: int
    wolves: int
    gods: int
    villagers: int
    seats: list[SeatInfo] = Field(default_factory=list)


class Deal(Directive):
    kind: Literal["ww_deal"] = "ww_deal"
    seats: list[SeatInfo]


class NightStart(Directive):
    kind: Literal["ww_night_start"] = "ww_night_start"
    day: int


class WolfVote(BaseModel):
    seat_no: int
    target_no: int
    speech: str
    monologue: str


class WolfKill(Directive):
    """当夜狼刀裁决：最终刀口 + 各狼提议明细（god-view，给观众看的密谋）。"""

    kind: Literal["ww_wolf_kill"] = "ww_wolf_kill"
    day: int
    target_no: int
    votes: list[WolfVote]


class SeerCheck(Directive):
    kind: Literal["ww_seer_check"] = "ww_seer_check"
    day: int
    seat_no: int
    target_no: int
    result: Literal["good", "werewolf"]
    monologue: str


class WitchAction(Directive):
    kind: Literal["ww_witch_action"] = "ww_witch_action"
    day: int
    seat_no: int
    save: bool
    poison_target_no: int | None
    monologue: str


class DeathInfo(BaseModel):
    seat_no: int
    role: str
    reason: str
    day: int


class DeathResolved(Directive):
    kind: Literal["ww_death_resolved"] = "ww_death_resolved"
    day: int
    deaths: list[DeathInfo]


class RoleReveal(Directive):
    kind: Literal["ww_role_reveal"] = "ww_role_reveal"
    day: int
    seat_no: int
    role: str
    reason: str


class IdiotReveal(Directive):
    kind: Literal["ww_idiot_reveal"] = "ww_idiot_reveal"
    day: int
    seat_no: int


class Speech(Directive):
    kind: Literal["ww_speech"] = "ww_speech"
    day: int | None = None
    phase: Literal["discussion", "last_words", "hunter", "banter"]
    seat_no: int
    text: str
    monologue: str


class ExileVote(BaseModel):
    seat_no: int
    target_no: int
    monologue: str


class VoteReveal(Directive):
    kind: Literal["ww_vote_reveal"] = "ww_vote_reveal"
    day: int
    votes: list[ExileVote]
    exiled: int | None
    tied: bool


class HunterShot(Directive):
    kind: Literal["ww_hunter_shot"] = "ww_hunter_shot"
    day: int
    hunter_no: int
    target_no: int | None
    text: str
    monologue: str


class GameOver(Directive):
    kind: Literal["ww_game_over"] = "ww_game_over"
    winner: Literal["good", "werewolf"]
    win_reason: str
    day: int
    seats: list[SeatInfo]
    deaths: list[DeathInfo]


class DeathLog(TimelineEntry):
    kind: Literal["ww_death"] = "ww_death"
    day: int
    seat_no: int
    role: str
    reason: str


class VoteLog(TimelineEntry):
    kind: Literal["ww_vote"] = "ww_vote"
    day: int
    exiled: int | None
    tied: bool
    votes: list[ExileVote]


class WerewolfScene(BaseScene):
    game: str = "werewolf"
    board_name: str = ""
    seats: list[SeatInfo] = Field(default_factory=list)
    dealt: bool = False
    day: int = 0
    phase: Literal["opening", "night", "day", "postgame"] = "opening"
    speaking: int | None = None
    night_target: int | None = None
    last_exiled: int | None = None
    winner: str | None = None
    win_reason: str | None = None
    deaths: list[DeathInfo] = Field(default_factory=list)
    timeline: list[SerializeAsAny[TimelineEntry]] = Field(default_factory=list)
