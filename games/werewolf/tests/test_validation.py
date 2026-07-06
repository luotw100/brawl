from __future__ import annotations

import random
from collections import Counter

import pytest

from werewolf.registry import Registry
from werewolf.settings import SettingsError, WerewolfSettings
from werewolf.state import Role
from werewolf.validation import resolve_and_validate


def test_seed_makes_setup_deterministic(registry: Registry) -> None:
    settings = WerewolfSettings(player="random")
    a = resolve_and_validate(settings, registry, random.Random(42))
    b = resolve_and_validate(settings, registry, random.Random(42))
    assert (a.cast, a.strategies, a.models, a.roles) == (b.cast, b.strategies, b.models, b.roles)


def test_setup_role_pool_and_snapshots(registry: Registry) -> None:
    setup = resolve_and_validate(WerewolfSettings(player="random"), registry, random.Random(0))
    assert Counter(setup.roles) == Counter(
        {
            Role.WEREWOLF: 4,
            Role.SEER: 1,
            Role.WITCH: 1,
            Role.HUNTER: 1,
            Role.IDIOT: 1,
            Role.VILLAGER: 4,
        }
    )
    assert len(setup.cast) == len(setup.strategies) == len(setup.models) == 12
    assert len(setup.names) == len(setup.voices) == len(setup.self_hosted_voices) == len(setup.voice_designs) == 12
    assert setup.to_meta()["board"] == "pre-witch-hunter-idiot-12"


def test_explicit_lists_validated(registry: Registry) -> None:
    cast = [registry.persona_ids()[i % len(registry.persona_ids())] for i in range(12)]
    strategies = [registry.strategy_ids()[0]] * 12
    models = [registry.model_names()[0]] * 12
    setup = resolve_and_validate(WerewolfSettings(player="random", cast=cast, strategies=strategies, models=models), registry, random.Random(0))
    assert (setup.cast, setup.strategies, setup.models) == (cast, strategies, models)

    with pytest.raises(SettingsError):
        resolve_and_validate(WerewolfSettings(player="random", cast=["missing"] * 12), registry, random.Random(0))
    with pytest.raises(SettingsError):
        resolve_and_validate(WerewolfSettings(player="random", strategies=["missing"] * 12), registry, random.Random(0))
    with pytest.raises(SettingsError):
        resolve_and_validate(WerewolfSettings(player="random", models=["missing"] * 12), registry, random.Random(0))


def test_random_backend_skips_api_key_check(registry: Registry) -> None:
    model_name = registry.model_names()[0]
    setup = resolve_and_validate(WerewolfSettings(player="random", models=[model_name] * 12), registry, random.Random(0))
    assert setup.models == [model_name] * 12
