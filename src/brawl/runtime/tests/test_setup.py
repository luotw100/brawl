"""brawl.runtime.setup 的边界解析：人设双模式、主持人、流派、模型、replay 目录防覆盖。"""

from __future__ import annotations

import random

import pytest

from brawl.games.avalon.registry import DATA_DIR as _DATA
from brawl.runtime import setup as core_setup
from brawl.runtime.registry import load_registry


class _Err(Exception):
    pass


def _registry(personas: list[str] | None = None):
    """用 avalon 的真实 data 造 registry；personas 参数仅文档化（这里用全量）。"""
    return load_registry(_DATA)


def test_explicit_cast_validated_and_kept_in_order() -> None:
    reg = _registry()
    ids = reg.persona_ids()[:3]
    out = core_setup.resolve_personas(ids, players=3, registry=reg, rng=random.Random(1), error=_Err)
    assert out == ids


def test_unknown_persona_rejected() -> None:
    reg = _registry()
    with pytest.raises(_Err, match="未知人设"):
        core_setup.resolve_personas(["nobody"], players=1, registry=reg, rng=random.Random(1), error=_Err)


def test_default_sampling_is_unique_when_pool_is_big_enough() -> None:
    reg = _registry()
    out = core_setup.resolve_personas(None, players=5, registry=reg, rng=random.Random(1), error=_Err)
    assert len(out) == 5 and len(set(out)) == 5


def test_unique_mode_rejects_oversized_game() -> None:
    reg = _registry()
    n = len(reg.persona_ids())
    with pytest.raises(_Err, match="不够"):
        core_setup.resolve_personas(None, players=n + 1, registry=reg, rng=random.Random(1), error=_Err)


def test_repeat_mode_fills_up_to_players() -> None:
    """狼人杀 12 人局模式：人设池不足时先用满再复用，绝不报错。"""
    reg = _registry()
    n = len(reg.persona_ids())
    out = core_setup.resolve_personas(None, players=n + 2, registry=reg, rng=random.Random(1), error=_Err, allow_repeat=True)
    assert len(out) == n + 2
    assert set(out) == set(reg.persona_ids())  # 池子先用满


def test_repeat_mode_still_unique_when_pool_suffices() -> None:
    reg = _registry()
    out = core_setup.resolve_personas(None, players=4, registry=reg, rng=random.Random(1), error=_Err, allow_repeat=True)
    assert len(set(out)) == 4


def test_resolve_host_default_and_unknown() -> None:
    reg = _registry()
    assert core_setup.resolve_host(None, registry=reg, default="herald", error=_Err) == "herald"
    with pytest.raises(_Err, match="未知主持人"):
        core_setup.resolve_host("nobody", registry=reg, default="herald", error=_Err)


def test_check_new_replay_refuses_existing_dir(tmp_path) -> None:
    with pytest.raises(_Err, match="已存在"):
        core_setup.check_new_replay(tmp_path, error=_Err)
    core_setup.check_new_replay(tmp_path / "new", error=_Err)  # 不存在则放行
    core_setup.check_new_replay(None, error=_Err)


def test_resolve_models_rejects_unknown() -> None:
    reg = _registry()
    with pytest.raises(_Err, match="未知模型"):
        core_setup.resolve_models(["no-such-model"], players=1, registry=reg, rng=random.Random(1), error=_Err)


def test_resolve_strategies_rejects_unknown() -> None:
    reg = _registry()
    with pytest.raises(_Err, match="未知流派"):
        core_setup.resolve_strategies(["no-such-strategy"], players=1, registry=reg, rng=random.Random(1), error=_Err)
