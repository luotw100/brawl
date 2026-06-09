"""AvalonSettings：配置唯一真源，本身即 CLI。

两类约束分两处：字段内在/形状校验在这里的 pydantic validator（players 在规则表内、
cast 长度匹配）；跨引用/世界一致性在 validation.py。本文件验前者 + 来源优先级
（直接 kwargs > CLI --xxx > env BRAWL_AVALON_*）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from avalon.settings import AvalonSettings

# ---------------------------------------------------------------- 形状校验

def test_side_counts_follow_rules_table() -> None:
    assert (AvalonSettings(players=5).good, AvalonSettings(players=5).evil) == (3, 2)
    assert (AvalonSettings(players=10).good, AvalonSettings(players=10).evil) == (6, 4)


@pytest.mark.parametrize("players", [4, 11])
def test_players_out_of_table_rejected(players: int) -> None:
    with pytest.raises(ValidationError):
        AvalonSettings(players=players)


def test_cast_length_must_match_players() -> None:
    with pytest.raises(ValidationError):
        AvalonSettings(players=5, cast=["fox"])


def test_strategies_and_models_length_must_match_players() -> None:
    with pytest.raises(ValidationError):
        AvalonSettings(players=5, strategies=["standard"])
    with pytest.raises(ValidationError):
        AvalonSettings(players=5, models=["a", "b"])


def test_morgana_requires_percival() -> None:
    """莫甘娜的伪装只对派西维尔有意义：单开莫甘娜直接拒绝。"""
    with pytest.raises(ValidationError):
        AvalonSettings(players=7, morgana=True)
    assert AvalonSettings(players=7, percival=True, morgana=True).morgana is True


def test_evil_specials_must_fit_evil_slots() -> None:
    """5 人局红方只有 2 个名额：刺客 + 莫德雷德 + 奥伯伦 = 3 超员，拒绝。"""
    with pytest.raises(ValidationError):
        AvalonSettings(players=5, mordred=True, oberon=True)
    assert AvalonSettings(players=7, mordred=True, oberon=True).evil_specials == 3


def test_concurrency_must_be_positive() -> None:
    """并发上限是信号量容量，至少为 1。"""
    with pytest.raises(ValidationError):
        AvalonSettings(players=5, concurrency=0)
    assert AvalonSettings(players=5, concurrency=3).concurrency == 3


# ---------------------------------------------------------------- 来源优先级

def test_kwargs_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """直接构造 kwargs 优先级最高，压过环境变量（代码/测试照常生效）。"""
    monkeypatch.setenv("BRAWL_AVALON_PLAYERS", "8")
    assert AvalonSettings(players=6).players == 6


def test_env_applies_when_no_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 kwargs/CLI 覆盖时，env BRAWL_AVALON_* 生效。"""
    monkeypatch.setenv("BRAWL_AVALON_PLAYERS", "8")
    assert AvalonSettings().players == 8


def test_cli_beats_env_via_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """走 load(cli_args=...)：CLI --players 压过 env（直接 kwargs > CLI > env）。"""
    monkeypatch.setenv("BRAWL_AVALON_PLAYERS", "8")
    s = AvalonSettings.load(cli_args=["--players", "6"], config_file=None)
    assert s.players == 6
