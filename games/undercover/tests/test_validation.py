"""validation.resolve_and_validate：配置的边界。

跑任何对局工作前调一次，把"这一局到底是什么"摇定成确定的 GameSetup，并把所有"跨引用 /
世界一致性"约束一次性查清（fail-fast）。守不变量 #3（开局随机前置到边界）：cast/models
缺省补齐、身份/词摇定，下游拿到的是确定值；非法引用、缺 key、replay 目录已存在都在这里炸。
"""

from __future__ import annotations

import random

import pytest

from undercover.registry import Registry
from undercover.settings import SettingsError, UndercoverSettings
from undercover.state import Role
from undercover.validation import resolve_and_validate


def test_defaults_resolved_and_roles_shuffled(registry: Registry) -> None:
    """缺省 cast/models 由 registry 补齐；身份摇定且卧底数恰为 settings.undercover。"""
    settings = UndercoverSettings(player="random", players=6, undercover=2)
    setup = resolve_and_validate(settings, registry, random.Random(0))

    assert len(setup.cast) == 6
    assert len(setup.models) == 6
    assert all(pid in registry.personas for pid in setup.cast)
    assert all(m in registry.models for m in setup.models)
    assert setup.roles.count(Role.UNDERCOVER) == 2
    assert setup.roles.count(Role.CIVILIAN) == 4
    assert setup.word_pair in registry.word_pairs or tuple(reversed(setup.word_pair)) in registry.word_pairs


def test_seed_makes_setup_deterministic(registry: Registry) -> None:
    """同一 seed → 同一开局方案（replay 可复盘的根基）。"""
    settings = UndercoverSettings(player="random", players=6, undercover=1)
    a = resolve_and_validate(settings, registry, random.Random(42))
    b = resolve_and_validate(settings, registry, random.Random(42))
    assert (a.cast, a.models, a.roles, a.word_pair) == (b.cast, b.models, b.roles, b.word_pair)


def test_explicit_word_pair_kept_as_is(registry: Registry) -> None:
    """显式 --word-pair 照用（B 固定为卧底词），不再摇方向。"""
    settings = UndercoverSettings(player="random", players=6, undercover=1, word_pair=("苹果", "梨"))
    setup = resolve_and_validate(settings, registry, random.Random(0))
    assert setup.word_pair == ("苹果", "梨")


def test_unknown_persona_raises(registry: Registry) -> None:
    settings = UndercoverSettings(
        player="random", players=6, undercover=1,
        cast=["不存在", "janitor", "diva", "shy", "shade", "honest"],
    )
    with pytest.raises(SettingsError):
        resolve_and_validate(settings, registry, random.Random(0))


def test_strategies_default_all_standard(registry: Registry) -> None:
    """缺省 --strategies → 全员 standard（流派的中性基线），且随 setup 入 meta。"""
    settings = UndercoverSettings(player="random", players=6, undercover=1)
    setup = resolve_and_validate(settings, registry, random.Random(0))
    assert setup.strategies == ["standard"] * 6
    assert setup.to_meta()["strategies"] == ["standard"] * 6


def test_explicit_strategies_kept_and_validated(registry: Registry) -> None:
    """显式 --strategies 照用；未知流派 id 在边界就炸。"""
    mix = ["charge", "turtle", "bait", "chaos", "standard", "standard"]
    settings = UndercoverSettings(player="random", players=6, undercover=1, strategies=mix)
    assert resolve_and_validate(settings, registry, random.Random(0)).strategies == mix

    bad = UndercoverSettings(player="random", players=6, undercover=1, strategies=["没这个流派"] * 6)
    with pytest.raises(SettingsError):
        resolve_and_validate(bad, registry, random.Random(0))


def test_unknown_model_raises(registry: Registry) -> None:
    settings = UndercoverSettings(
        player="random", players=6, undercover=1, models=["没这个模型"] * 6,
    )
    with pytest.raises(SettingsError):
        resolve_and_validate(settings, registry, random.Random(0))


def test_replay_dir_already_exists_raises(registry: Registry, tmp_path) -> None:
    """--replay 目录已存在即报错（不覆盖、无 --force）。"""
    settings = UndercoverSettings(player="random", players=6, undercover=1, replay=tmp_path)
    with pytest.raises(SettingsError):
        resolve_and_validate(settings, registry, random.Random(0))


def test_llm_backend_missing_api_key_raises(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """llm 后端下，用到的模型缺 api_key 必须在边界就炸，而非跑到一半首次调用才炸。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = UndercoverSettings(player="llm", players=6, undercover=1)
    with pytest.raises(SettingsError):
        resolve_and_validate(settings, registry, random.Random(0))


def test_random_backend_skips_api_key_check(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """random 后端不调模型，即便没 key 也能解析通过（离线 mock 可跑通/测试）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = UndercoverSettings(player="random", players=6, undercover=1)
    setup = resolve_and_validate(settings, registry, random.Random(0))
    assert len(setup.cast) == 6
