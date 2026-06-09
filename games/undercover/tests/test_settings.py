"""UndercoverSettings：配置唯一真源，本身即 CLI（不变量 #8）。

两类约束分两处：字段内在/形状校验在这里的 pydantic validator（数量自洽、cast/models 长度
匹配）；跨引用/世界一致性在 validation.py。本文件验前者 + 来源优先级
（直接 kwargs > CLI --xxx > env BRAWL_UNDERCOVER_*）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from undercover.settings import UndercoverSettings

# ---------------------------------------------------------------- 形状校验

def test_civilians_property() -> None:
    s = UndercoverSettings(player="random", players=6, undercover=1, blank=0)
    assert s.civilians == 5


def test_blank_not_implemented() -> None:
    """MVP 未实现白板，blank≠0 直接拒绝。"""
    with pytest.raises(NotImplementedError):
        UndercoverSettings(player="random", players=6, undercover=1, blank=1)


def test_too_few_civilians_rejected() -> None:
    with pytest.raises(ValidationError):
        UndercoverSettings(player="random", players=3, undercover=2)  # 平民=1 < 2


def test_at_least_one_undercover() -> None:
    with pytest.raises(ValidationError):
        UndercoverSettings(player="random", players=6, undercover=0)


def test_cast_length_must_match_players() -> None:
    with pytest.raises(ValidationError):
        UndercoverSettings(player="random", players=6, undercover=1, cast=["fox"])


def test_models_length_must_match_players() -> None:
    with pytest.raises(ValidationError):
        UndercoverSettings(player="random", players=6, undercover=1, models=["a", "b"])


# ---------------------------------------------------------------- 来源优先级

def test_kwargs_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """直接构造 kwargs 优先级最高，压过环境变量（代码/测试照常生效）。"""
    monkeypatch.setenv("BRAWL_UNDERCOVER_PLAYERS", "8")
    assert UndercoverSettings(player="random", players=4, undercover=1).players == 4


def test_env_applies_when_no_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 kwargs/CLI 覆盖时，env BRAWL_UNDERCOVER_* 生效。"""
    monkeypatch.setenv("BRAWL_UNDERCOVER_PLAYERS", "8")
    assert UndercoverSettings(player="random", undercover=1).players == 8


def test_cli_beats_env_via_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """走 load(cli_args=...)：CLI --players 压过 env（直接 kwargs > CLI > env）。"""
    monkeypatch.setenv("BRAWL_UNDERCOVER_PLAYERS", "8")
    s = UndercoverSettings.load(cli_args=["--players", "5", "--player", "random"], config_file=None)
    assert s.players == 5
