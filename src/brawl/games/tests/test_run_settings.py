"""RunSettings/ResumeSettings 的字段内在校验（不依赖 registry 的都在这层挡住）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brawl.games.avalon.rules import DEFAULT_BOARD as AVALON_BOARD
from brawl.games.run import ResumeSettings, RunSettings
from brawl.games.werewolf.rules import DEFAULT_BOARD as WEREWOLF_BOARD


def _run_kwargs(**over):
    base = {"board": AVALON_BOARD, "replay": "/tmp/nonexistent-replay-for-settings-test"}
    return base | over


def test_board_must_be_registered() -> None:
    with pytest.raises(ValidationError, match="未知板子"):
        RunSettings(**_run_kwargs(board="no-such-board"))


def test_both_production_boards_accepted() -> None:
    assert RunSettings(**_run_kwargs()).board == AVALON_BOARD
    assert RunSettings(**_run_kwargs(board=WEREWOLF_BOARD)).board == WEREWOLF_BOARD


def test_cast_length_must_equal_board_players() -> None:
    with pytest.raises(ValidationError, match="--cast 长度 4 必须等于板子人数 10"):
        RunSettings(**_run_kwargs(cast=["fox"] * 4))


def test_models_length_must_equal_board_players() -> None:
    with pytest.raises(ValidationError, match="--models 长度 2 必须等于板子人数 12"):
        RunSettings(**_run_kwargs(board=WEREWOLF_BOARD, models=["a", "b"]))


def test_concurrency_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="--concurrency"):
        RunSettings(**_run_kwargs(concurrency=0))


def test_defaults_are_sane() -> None:
    s = RunSettings(**_run_kwargs())
    assert s.backend == "opencode" and s.render is True and s.seed is None


def test_replay_is_required() -> None:
    with pytest.raises(ValidationError):
        RunSettings(board=AVALON_BOARD)


def test_resume_takes_only_replay() -> None:
    s = ResumeSettings(replay="/tmp/game")
    assert str(s.replay) == "/tmp/game"
    assert set(ResumeSettings.model_fields) == {"replay"}
