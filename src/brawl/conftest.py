"""测试装配：板子注册表隔离——cli 装配是否已发生不影响任何测试文件（收集期 import 顺序无关）。"""

from __future__ import annotations

import pytest

from brawl.games import run as games_run
from brawl.games.avalon import rules as avalon_rules
from brawl.games.avalon import run as avalon_run
from brawl.games.werewolf import rules as werewolf_rules
from brawl.games.werewolf import run as werewolf_run


@pytest.fixture(autouse=True)
def _board_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    avalon = avalon_rules.MERLIN_ASSASSIN_PERCIVAL_MORGANA_MORDRED_OBERON_LADY_10
    werewolf = werewolf_rules.PRE_WITCH_HUNTER_IDIOT_12
    monkeypatch.setattr(
        games_run,
        "_BOARDS",
        {
            avalon.id: games_run.BoardEntry(id=avalon.id, game="avalon", players=avalon.players, title=avalon.name, run=avalon_run.run_game),
            werewolf.id: games_run.BoardEntry(id=werewolf.id, game="werewolf", players=werewolf.players, title=werewolf.name, run=werewolf_run.run_game),
        },
    )
