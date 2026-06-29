"""狼人杀规则表。

第一版只开放 12 人预女猎白，但状态机消费的是 BoardSpec：角色池、神民划分、胜负规则
和昼夜流程都从这里取。以后加狼王魔术师、奇迹商人时，先扩展规则表和技能节点，不把
新板子的常量散进主流程。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Side(StrEnum):
    GOOD = "good"
    WEREWOLF = "werewolf"


class Role(StrEnum):
    WEREWOLF = "werewolf"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    IDIOT = "idiot"
    VILLAGER = "villager"

    @property
    def side(self) -> Side:
        return Side.WEREWOLF if self is Role.WEREWOLF else Side.GOOD

    @property
    def team_kind(self) -> Literal["wolf", "god", "villager"]:
        if self is Role.WEREWOLF:
            return "wolf"
        if self is Role.VILLAGER:
            return "villager"
        return "god"


@dataclass(frozen=True)
class BoardSpec:
    id: str
    name: str
    players: int
    roles: tuple[Role, ...]
    night_order: tuple[str, ...]
    reveal_on_death: bool
    last_words: bool
    win_condition: Literal["slaughter_side"]

    @property
    def wolves(self) -> int:
        return sum(1 for r in self.roles if r is Role.WEREWOLF)

    @property
    def gods(self) -> int:
        return sum(1 for r in self.roles if r.team_kind == "god")

    @property
    def villagers(self) -> int:
        return sum(1 for r in self.roles if r is Role.VILLAGER)


PRE_WITCH_HUNTER_IDIOT_12 = BoardSpec(
    id="pre-witch-hunter-idiot-12",
    name="预女猎白12人场",
    players=12,
    roles=(
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.WEREWOLF,
        Role.SEER,
        Role.WITCH,
        Role.HUNTER,
        Role.IDIOT,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
        Role.VILLAGER,
    ),
    night_order=("wolves", "seer", "witch"),
    reveal_on_death=True,
    last_words=True,
    win_condition="slaughter_side",
)

BOARDS: dict[str, BoardSpec] = {PRE_WITCH_HUNTER_IDIOT_12.id: PRE_WITCH_HUNTER_IDIOT_12}
DEFAULT_BOARD = PRE_WITCH_HUNTER_IDIOT_12.id


ROLE_LABEL = {
    Role.WEREWOLF: "狼人",
    Role.SEER: "预言家",
    Role.WITCH: "女巫",
    Role.HUNTER: "猎人",
    Role.IDIOT: "白痴",
    Role.VILLAGER: "平民",
}

SIDE_LABEL = {Side.GOOD: "好人阵营", Side.WEREWOLF: "狼人阵营"}


def board_by_id(board_id: str) -> BoardSpec:
    return BOARDS[board_id]


def living_wolves(roles: list[tuple[Role, bool]]) -> int:
    return sum(1 for role, alive in roles if alive and role is Role.WEREWOLF)


def living_gods(roles: list[tuple[Role, bool]]) -> int:
    return sum(1 for role, alive in roles if alive and role.team_kind == "god")


def living_villagers(roles: list[tuple[Role, bool]]) -> int:
    return sum(1 for role, alive in roles if alive and role is Role.VILLAGER)

