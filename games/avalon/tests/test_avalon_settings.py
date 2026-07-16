"""AvalonSettings 的字段内在校验（形状约束，不依赖 registry 的都在这层挡住）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from avalon.settings import AvalonSettings


def test_players_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        AvalonSettings(players=4)
    with pytest.raises(ValidationError):
        AvalonSettings(players=11)


def test_morgana_requires_percival() -> None:
    with pytest.raises(ValidationError, match="莫甘娜需要派西维尔"):
        AvalonSettings(players=10, morgana=True, percival=False)
    ok = AvalonSettings(players=10, morgana=True, percival=True)
    assert ok.morgana and ok.percival


def test_evil_specials_cannot_exceed_evil_seats() -> None:
    # 5 人局红方只有 2 个名额：刺客 + 莫甘娜 + 莫德雷德 = 3 超额
    with pytest.raises(ValidationError, match="红方特殊角色"):
        AvalonSettings(players=5, percival=True, morgana=True, mordred=True)


def test_cast_length_must_equal_players() -> None:
    with pytest.raises(ValidationError, match="cast 长度"):
        AvalonSettings(players=5, cast=["fox"] * 4)


def test_models_length_must_equal_players() -> None:
    with pytest.raises(ValidationError, match="models 长度"):
        AvalonSettings(players=5, models=["a", "b"])


def test_opencode_player_requires_replay() -> None:
    with pytest.raises(ValidationError, match="--player opencode 必须提供 --replay"):
        AvalonSettings(player="opencode")


def test_resume_requires_replay() -> None:
    with pytest.raises(ValidationError, match="--resume 需配合 --replay"):
        AvalonSettings(resume=True)


def test_concurrency_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="concurrency"):
        AvalonSettings(concurrency=0)


def test_defaults_are_sane() -> None:
    s = AvalonSettings()
    assert s.players == 10 and s.player == "random" and s.render is True
    assert s.evil + s.good == s.players
