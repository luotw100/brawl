"""Avalon 新 runtime 入口：用 core.sr.loop 驱动 AvalonRules。"""

from __future__ import annotations

import argparse
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from avalon.random_actor import AvalonRandomActor
from avalon.registry import load_registry
from avalon.rules_engine import AvalonRules, AvalonState
from avalon.settings import AvalonSettings
from avalon.validation import GameSetup, resolve_and_validate, validate_resume
from core.opencode_runtime import (
    OpenCodeInstallation,
    open_opencode_runtime,
    prepare_opencode_installation,
)
from core.sr.actor import OpenCodeActor
from core.sr.journal import open_journal
from core.sr.loop import resume, run, run_async_main
from core.sr.opencode_backend import OpenCodeBackend
from core.threads import SeatThread


def _build_seats(setup: GameSetup) -> list[Any]:
    """setup 已解析+摇定，这里只管拼装 Seat 并把 role/intel 填进去。"""
    from avalon import rules
    from avalon.state import Seat

    return [
        Seat(
            no=i + 1,
            persona_id=setup.cast[i],
            strategy_id=setup.strategies[i],
            model=setup.models[i],
            role=role,
            intel=rules.night_intel(setup.roles, i + 1),
        )
        for i, role in enumerate(setup.roles)
    ]


async def run_game(settings: AvalonSettings) -> AvalonState:
    registry = load_registry()
    if settings.resume:
        return await _resume_game(settings, registry)
    return await _fresh_game(settings, registry)


async def _fresh_game(settings: AvalonSettings, registry: Any) -> AvalonState:
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    installation = _prepare_runtime(settings)
    journal = open_journal(settings.replay, resume=False)
    journal.write_meta(_build_meta(registry, setup))
    for seat in _build_seats(setup):
        src = registry.avatar_path(seat.model)
        if src is not None:
            journal.write_avatar(seat.no, src)

    async with _open_actors(settings, registry, rng, setup, installation) as actors:
        rules = AvalonRules(registry, settings)
        threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
        return await run(rules, setup, actors, journal, threads)


async def _resume_game(settings: AvalonSettings, registry: Any) -> AvalonState:
    meta = validate_resume(settings, registry)
    setup = _setup_from_meta(meta)
    installation = _prepare_runtime(settings)
    journal = open_journal(settings.replay, resume=True)
    async with _open_actors(settings, registry, random.Random(settings.seed), setup, installation) as actors:
        rules = AvalonRules(registry, settings)
        threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
        restored = await resume(rules, actors={}, journal=journal, threads=threads, setup=setup)
        if not rules.is_terminal(restored):
            return await run(rules, setup, actors, journal, threads, state=restored)
        return restored


@asynccontextmanager
async def _open_actors(
    settings: AvalonSettings,
    registry: Any,
    rng: random.Random,
    setup: GameSetup,
    installation: OpenCodeInstallation | None,
) -> AsyncIterator[dict[int, Any]]:
    if settings.player == "random":
        actors = {no: AvalonRandomActor(rng) for no in range(1, len(setup.roles) + 1)}
        try:
            yield actors
        finally:
            for a in actors.values():
                await a.close()
        return

    assert installation is not None and settings.replay is not None
    async with open_opencode_runtime(installation, settings.replay, len(setup.roles)) as connection:
        backend = OpenCodeBackend(
            connection=connection,
            default_model=settings.opencode_model,
            seat_models=_build_seat_models(registry, setup),
        )
        actors = {no: OpenCodeActor(backend) for no in range(1, len(setup.roles) + 1)}
        try:
            yield actors
        finally:
            for a in actors.values():
                await a.close()


def _build_seat_models(registry: Any, setup: GameSetup) -> dict[int, str]:
    seat_models: dict[int, str] = {}
    for i, name in enumerate(setup.models, start=1):
        spec = registry.models.get(name)
        if spec is not None and spec.opencode_model:
            seat_models[i] = spec.opencode_model
    return seat_models


def _prepare_runtime(settings: AvalonSettings) -> OpenCodeInstallation | None:
    return prepare_opencode_installation() if settings.player == "opencode" else None


def _build_meta(registry: Any, setup: GameSetup) -> dict[str, Any]:
    return {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "player": "avalon",
        "setup": setup.to_meta(),
        "model_labels": {
            m: registry.models[m].display
            for m in dict.fromkeys(setup.models)
            if m and m in registry.models and registry.models[m].display
        },
    }


def _setup_from_meta(meta: dict[str, Any]) -> GameSetup:
    from avalon.state import Role

    s = meta["setup"]
    return GameSetup(
        cast=s["cast"],
        host_cast=s["host_cast"],
        names=s["names"],
        host_name=s["host_name"],
        voices=s["voices"],
        host_voice=s["host_voice"],
        self_hosted_voices=s["self_hosted_voices"],
        host_self_hosted_voice=s["host_self_hosted_voice"],
        voice_designs=s["voice_designs"],
        host_voice_design=s["host_voice_design"],
        strategies=s["strategies"],
        models=s["models"],
        roles=[Role(r) for r in s["roles"]],
        first_leader=s["first_leader"],
        lady_holder=s["lady_holder"],
    )


def main(argv: list[str]) -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", dest="config", type=Path, default=None, metavar="PATH", help="JSON 配置文件路径")
    args, rest = pre.parse_known_args(argv)
    settings = AvalonSettings.load(cli_args=rest, config_file=args.config)
    run_async_main(run_game(settings))
