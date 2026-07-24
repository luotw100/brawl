"""狼人杀板子与规则常数的唯一静态定义。角色/阵营词汇定义在 state.py。"""

from __future__ import annotations

from dataclasses import dataclass

from brawl.games.werewolf.state import Role, Side


@dataclass(frozen=True)
class BoardSpec:
    """一个板子：角色多重集 + 它的纯派生。其余玩法规则是本游戏固定规则，不在此。"""

    id: str
    name: str
    roles: tuple[Role, ...]

    @property
    def players(self) -> int:
        return len(self.roles)

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

WIN_REASON_LABEL = {
    "all_wolves_dead": "狼人全部出局",
    "all_gods_dead": "神职全部出局",
    "all_villagers_dead": "平民全部出局",
}


def board_by_id(board_id: str) -> BoardSpec:
    return BOARDS[board_id]


# 屠边判负的三个计数（入参是 (角色, 是否存活) 列表，纯函数，judge 侧零随机）。
def living_wolves(roles: list[tuple[Role, bool]]) -> int:
    return sum(1 for role, alive in roles if alive and role is Role.WEREWOLF)


def living_gods(roles: list[tuple[Role, bool]]) -> int:
    return sum(1 for role, alive in roles if alive and role.team_kind == "god")


def living_villagers(roles: list[tuple[Role, bool]]) -> int:
    return sum(1 for role, alive in roles if alive and role is Role.VILLAGER)
