from __future__ import annotations

from collections import Counter

from werewolf import rules
from werewolf.state import Role


def test_pre_witch_hunter_idiot_12_board_shape() -> None:
    board = rules.PRE_WITCH_HUNTER_IDIOT_12
    assert board.players == 12
    assert Counter(board.roles) == Counter(
        {
            Role.WEREWOLF: 4,
            Role.SEER: 1,
            Role.WITCH: 1,
            Role.HUNTER: 1,
            Role.IDIOT: 1,
            Role.VILLAGER: 4,
        }
    )
    assert board.wolves == 4
    assert board.gods == 4
    assert board.villagers == 4

