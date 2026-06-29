"""狼人杀状态与结构化决策。

座位号一律 1 起。隐藏身份、狼队互认、预言家验人、女巫药瓶状态只进入对应席位的
PlayerView/私有线程；公开账本只记录桌面可见事实。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from core.threads import SeatThread
from werewolf.rules import Role, Side

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Seat(BaseModel):
    no: int
    persona_id: str
    strategy_id: str
    model: str
    role: Role
    alive: bool = True
    can_vote: bool = True
    revealed: bool = False

    @property
    def side(self) -> Side:
        return self.role.side


class DeathRecord(BaseModel):
    seat_no: int
    role: Role
    reason: Literal["wolf_kill", "poison", "exile", "hunter_shot"]
    day: int


class SpeechRecord(BaseModel):
    day: int
    seat_no: int
    text: str


class VoteRecord(BaseModel):
    day: int
    votes: dict[int, int]
    exiled: int | None
    tied: bool


class SeerCheck(BaseModel):
    target_no: int
    side: Side
    day: int


class WolfKill(BaseModel):
    target_no: int
    speech: NonEmptyStr
    monologue: NonEmptyStr


class SeerAction(BaseModel):
    target_no: int
    monologue: NonEmptyStr


class WitchAction(BaseModel):
    save: bool = False
    poison_target_no: int | None = None
    monologue: NonEmptyStr


class Statement(BaseModel):
    speech: NonEmptyStr
    monologue: NonEmptyStr


class VoteAction(BaseModel):
    target_no: int
    monologue: NonEmptyStr


class HunterShot(BaseModel):
    target_no: int | None = None
    speech: NonEmptyStr
    monologue: NonEmptyStr


class GameState(BaseModel):
    seats: list[Seat]
    board: str
    threads: dict[int, SeatThread] = Field(default_factory=dict)
    day: int = 1
    phase: Literal["opening", "after_night", "after_vote"] = "opening"
    speeches: list[SpeechRecord] = Field(default_factory=list)
    votes: list[VoteRecord] = Field(default_factory=list)
    deaths: list[DeathRecord] = Field(default_factory=list)
    round_deaths: list[DeathRecord] = Field(default_factory=list)
    seer_checks: list[SeerCheck] = Field(default_factory=list)
    last_seer_check: SeerCheck | None = None
    wolf_votes: dict[int, int] = Field(default_factory=dict)
    wolf_target: int | None = None
    witch_saved: bool = False
    witch_poison_target: int | None = None
    witch_has_antidote: bool = True
    witch_has_poison: bool = True
    pending_hunter: int | None = None
    winner: str | None = None
    win_reason: str | None = None
    finished: bool = False

    @model_validator(mode="after")
    def _init_threads(self) -> GameState:
        if not self.threads:
            self.threads = {s.no: SeatThread() for s in self.seats}
        return self

    @property
    def attempt(self) -> int:
        return 1

    def living(self) -> list[Seat]:
        return [s for s in self.seats if s.alive]

    def living_nos(self) -> list[int]:
        return [s.no for s in self.living()]

    def seat(self, no: int) -> Seat:
        return self.seats[no - 1]
