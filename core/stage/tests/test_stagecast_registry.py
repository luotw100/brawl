"""StageGame registry：装配边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.stage import stagecast as registry_mod
from core.stage.stagecast import StageGame, game_for, register


def _bundle(game_id: str) -> StageGame:
    return StageGame(
        id=game_id, stagecast=lambda s, m, md: [], new_scene=lambda: None,
        apply=lambda sc, it: None, summary=lambda d, sc: ("", "", ""),
        is_opening_anchor=lambda d: False, terminal=lambda d: False,
        plan_path=Path("x.yaml"),
    )


def test_register_then_game_for(monkeypatch) -> None:
    monkeypatch.setattr(registry_mod, "_REGISTRY", {})
    register(_bundle("g1"))
    assert game_for("g1").id == "g1"


def test_duplicate_register_rejected(monkeypatch) -> None:
    monkeypatch.setattr(registry_mod, "_REGISTRY", {})
    register(_bundle("g1"))
    with pytest.raises(ValueError, match="重复登记"):
        register(_bundle("g1"))


def test_unknown_game_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(registry_mod, "_REGISTRY", {})
    with pytest.raises(ValueError, match="未登记的玩法"):
        game_for("ghost")
