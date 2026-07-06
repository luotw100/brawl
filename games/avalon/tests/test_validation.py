"""validation.resolve_and_validate：配置的边界。

跑任何对局工作前调一次，把"这一局到底是什么"摇定成确定的 GameSetup，并把所有"跨引用 /
世界一致性"约束一次性查清（fail-fast）：cast 缺省补齐、角色按规则表配池摇定、首任队长
摇定，下游拿到的是确定值；非法引用、replay 目录已存在都在这里炸。
"""

from __future__ import annotations

import random
from collections import Counter
from itertools import product

import pytest

from avalon import rules
from avalon.registry import Persona, Registry
from avalon.settings import AvalonSettings, SettingsError
from avalon.state import Role
from avalon.validation import resolve_and_validate
from core import setup as core_setup


@pytest.mark.parametrize("players", sorted(rules.TEAM_SIZES))
def test_role_pool_matches_rules_table(registry: Registry, players: int) -> None:
    """每个人数档：恰好 1 梅林 + 1 刺客，红方总数按规则表，其余忠臣。"""
    setup = resolve_and_validate(AvalonSettings(player="random", players=players), registry, random.Random(0))
    evil = rules.EVIL_COUNT[players]
    assert len(setup.roles) == players
    assert setup.roles.count(Role.MERLIN) == 1
    assert setup.roles.count(Role.ASSASSIN) == 1
    assert setup.roles.count(Role.MINION) == evil - 1
    assert setup.roles.count(Role.SERVANT) == players - evil - 1
    assert len(setup.cast) == len(setup.models) == players
    assert all(pid in registry.personas for pid in setup.cast)
    assert all(m in registry.models for m in setup.models)  # 缺省时从 provider 池随机分配
    assert len(setup.strategies) == players and all(s in registry.strategies for s in setup.strategies)  # 缺省放回随机
    assert 1 <= setup.first_leader <= players  # 座位号 1 起


@pytest.mark.parametrize("players", sorted(rules.TEAM_SIZES))
def test_role_pool_with_special_switches_exhaustive(registry: Registry, players: int) -> None:
    """6 个人数档 × 16 种开关组合**全枚举**：非法组合（莫甘娜无派西维尔、红方特殊角色
    超名额）在 settings 形状校验就炸；合法组合的角色多重集逐一对照独立算出的期望。"""
    from pydantic import ValidationError

    evil = rules.EVIL_COUNT[players]
    for percival, morgana, mordred, oberon in product([False, True], repeat=4):
        kwargs = {"player": "random", "players": players, "percival": percival, "morgana": morgana, "mordred": mordred, "oberon": oberon}
        evil_specials = 1 + morgana + mordred + oberon
        if (morgana and not percival) or evil_specials > evil:
            with pytest.raises(ValidationError):
                AvalonSettings(**kwargs)
            continue
        setup = resolve_and_validate(AvalonSettings(**kwargs), registry, random.Random(0))
        assert Counter(setup.roles) == Counter(
            {Role.MERLIN: 1, Role.PERCIVAL: int(percival), Role.SERVANT: players - evil - 1 - int(percival),
             Role.ASSASSIN: 1, Role.MORGANA: int(morgana), Role.MORDRED: int(mordred), Role.OBERON: int(oberon),
             Role.MINION: evil - evil_specials}
        ), f"players={players} 开关={percival, morgana, mordred, oberon} 角色池不对"


def test_seed_makes_setup_deterministic(registry: Registry) -> None:
    """同一 seed → 同一开局方案（replay 可复盘的根基）。"""
    settings = AvalonSettings(player="random", players=7)
    a = resolve_and_validate(settings, registry, random.Random(42))
    b = resolve_and_validate(settings, registry, random.Random(42))
    assert (a.cast, a.strategies, a.models, a.roles, a.first_leader) == (b.cast, b.strategies, b.models, b.roles, b.first_leader)


def test_default_model_sampling_decays_repeated_pick_weight(registry: Registry) -> None:
    """缺省模型分配仍有放回；同一模型每被抽中一次,后续权重乘 0.5。"""
    names = registry.model_names()[:2]
    tiny = Registry(
        models={name: registry.models[name] for name in names},
        personas=registry.personas,
        hosts=registry.hosts,
        strategies=registry.strategies,
        avatars_dir=registry.avatars_dir,
    )

    class RecordingRng:
        def __init__(self) -> None:
            self.weights: list[tuple[float, ...]] = []

        def choices(self, population: list[str], *, weights: list[float], k: int) -> list[str]:
            self.weights.append(tuple(weights))
            return [population[0]]

    rng = RecordingRng()
    assert core_setup.resolve_models(None, players=3, registry=tiny, rng=rng, error=SettingsError) == [names[0]] * 3
    assert rng.weights == [(1.0, 1.0), (0.5, 1.0), (0.25, 1.0)]


def test_explicit_cast_kept_and_validated(registry: Registry) -> None:
    """显式 --cast 照用；未知人设 id 在边界就炸。"""
    cast = ["shade", "fox", "diva", "shy", "honest"]
    setup = resolve_and_validate(AvalonSettings(player="random", players=5, cast=cast), registry, random.Random(0))
    assert setup.cast == cast

    bad = AvalonSettings(player="random", players=5, cast=["不存在", "fox", "diva", "shy", "honest"])
    with pytest.raises(SettingsError):
        resolve_and_validate(bad, registry, random.Random(0))


def test_host_cast_defaults_resolved_and_validated(registry: Registry) -> None:
    """主持人人设：缺省 herald；显式 --host-cast 照用并写进 setup；未知 id 开打前就炸。"""
    setup = resolve_and_validate(AvalonSettings(player="random", players=5), registry, random.Random(0))
    assert setup.host_cast == "herald"
    assert "host_cast" in setup.to_meta()

    explicit = resolve_and_validate(AvalonSettings(player="random", players=5, host_cast="herald"), registry, random.Random(0))
    assert explicit.host_cast == "herald"

    # 主持人库与玩家库分开：玩家人设不能当主持人（fox 是玩家），未知 id 同样炸。
    for bad in ("fox", "没这个主持人"):
        with pytest.raises(SettingsError):
            resolve_and_validate(AvalonSettings(player="random", players=5, host_cast=bad), registry, random.Random(0))


def test_voice_snapshot_for_tts_backends(registry: Registry) -> None:
    """TTS 音色随 replay 快照，cast.yaml 后续改动不影响老局。"""
    cast = ["shade", "fox", "diva", "shy", "honest"]
    setup = resolve_and_validate(AvalonSettings(player="random", players=5, cast=cast), registry, random.Random(0))
    assert len(setup.voice_designs) == 5
    assert setup.host_voice_design
    assert len(setup.self_hosted_voices) == 5
    assert setup.host_self_hosted_voice is not None


def test_explicit_strategies_and_models_kept_and_validated(registry: Registry) -> None:
    """显式 --strategies/--models 照用；未知 id 在边界就炸。"""
    strategies = ["prudent"] * 5
    models = ["oc-deepseek-v4-flash"] * 5
    setup = resolve_and_validate(AvalonSettings(player="random", players=5, strategies=strategies, models=models), registry, random.Random(0))
    assert (setup.strategies, setup.models) == (strategies, models)

    with pytest.raises(SettingsError):
        resolve_and_validate(AvalonSettings(player="random", players=5, strategies=["没这个流派"] * 5), registry, random.Random(0))
    with pytest.raises(SettingsError):
        resolve_and_validate(AvalonSettings(player="random", players=5, models=["没这个模型"] * 5), registry, random.Random(0))


def test_lady_holder_is_first_leaders_upstream_seat(registry: Registry) -> None:
    """湖中仙女首任持有者 = 首任队长上家（1-based 环绕：队长 1 号时上家是 N 号）。"""
    for seed in range(20):
        setup = resolve_and_validate(AvalonSettings(player="random", players=5, lady=True), registry, random.Random(seed))
        expected = setup.first_leader - 1 if setup.first_leader > 1 else 5
        assert setup.lady_holder == expected



def test_not_enough_personas_raises(registry: Registry) -> None:
    """人设库坐不满又没显式 --cast → 开打前就炸。"""
    tiny = Registry(
        models=registry.models,
        personas={p: Persona(id=p, name=p, tone="x") for p in ("a", "b", "c")},
        hosts=registry.hosts,
        strategies=registry.strategies,
        avatars_dir=registry.avatars_dir,
    )
    with pytest.raises(SettingsError):
        resolve_and_validate(AvalonSettings(player="random", players=5), tiny, random.Random(0))


def test_replay_dir_already_exists_raises(registry: Registry, tmp_path) -> None:
    """--replay 目录已存在即报错（不覆盖、无 --force）。"""
    settings = AvalonSettings(player="random", players=5, replay=tmp_path)
    with pytest.raises(SettingsError):
        resolve_and_validate(settings, registry, random.Random(0))



def test_random_backend_no_key_needed(registry: Registry) -> None:
    """random 后端不连模型，无需任何 key 即可解析通过。"""
    model_name = registry.model_names()[0]
    setup = resolve_and_validate(AvalonSettings(player="random", players=5, models=[model_name] * 5), registry, random.Random(0))
    assert len(setup.cast) == 5
