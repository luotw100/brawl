"""Werewolf 新 runtime 入口：用 core.sr.loop 驱动 WerewolfRules。"""

from __future__ import annotations

import argparse
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.opencode_runtime import (
    OpenCodeInstallation,
    open_opencode_runtime,
    prepare_opencode_installation,
)
from core.sr.actor import OpenCodeActor, RandomActor
from core.sr.console import ConsoleRenderer, InstantTypist
from core.sr.journal import open_journal
from core.sr.loop import resume, run, run_async_main
from core.sr.opencode_backend import OpenCodeBackend
from core.threads import SeatThread
from werewolf.registry import load_registry
from werewolf.rules import Role, board_by_id
from werewolf.rules_engine import WerewolfRules, WerewolfState
from werewolf.settings import WerewolfSettings
from werewolf.validation import GameSetup, resolve_and_validate, validate_resume


async def run_game(settings: WerewolfSettings) -> WerewolfState:
    registry = load_registry()
    if settings.resume:
        return await _resume_game(settings, registry)
    return await _fresh_game(settings, registry)


async def _fresh_game(settings: WerewolfSettings, registry: Any) -> WerewolfState:
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    installation = _prepare_runtime(settings)
    meta = _build_meta(registry, setup, settings)
    journal = open_journal(settings.replay, resume=False)
    journal.write_meta(meta)
    renderer = _build_renderer(settings, meta)
    for i, model_name in enumerate(setup.models, start=1):
        src = registry.avatar_path(model_name)
        if src is not None:
            journal.write_avatar(i, src)

    rules = WerewolfRules(registry, settings)
    async with _open_actors(settings, registry, setup, installation, rules) as actors:
        threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
        return await run(rules, setup, actors, journal, threads, renderer=renderer, max_concurrency=settings.concurrency)


async def _resume_game(settings: WerewolfSettings, registry: Any) -> WerewolfState:
    meta = validate_resume(settings, registry)
    setup = _setup_from_meta(meta)
    installation = _prepare_runtime(settings)
    journal = open_journal(settings.replay, resume=True)
    renderer = _build_renderer(settings, meta)
    rules = WerewolfRules(registry, settings)
    async with _open_actors(settings, registry, setup, installation, rules) as actors:
        threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
        restored = await resume(rules, actors={}, journal=journal, threads=threads, setup=setup)
        if not rules.is_terminal(restored):
            return await run(rules, setup, actors, journal, threads, renderer=renderer, state=restored, max_concurrency=settings.concurrency)
        return restored


@asynccontextmanager
async def _open_actors(
    settings: WerewolfSettings,
    registry: Any,
    setup: GameSetup,
    installation: OpenCodeInstallation | None,
    rules: WerewolfRules,
) -> AsyncIterator[dict[int, Any]]:
    if settings.player == "random":
        actors = {no: RandomActor(rules.random_action) for no in range(1, len(setup.roles) + 1)}
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


def _build_renderer(settings: WerewolfSettings, meta: dict[str, Any]) -> ConsoleRenderer | None:
    if not settings.render:
        return None
    return ConsoleRenderer(typist=InstantTypist(), meta=meta)


def _prepare_runtime(settings: WerewolfSettings) -> OpenCodeInstallation | None:
    return prepare_opencode_installation() if settings.player == "opencode" else None


def _build_meta(registry: Any, setup: GameSetup, settings: WerewolfSettings) -> dict[str, Any]:
    board = board_by_id(setup.board)
    return {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "game": "werewolf",  # stage registry 按此派发 plugin
        "player": settings.player,
        "setup": setup.to_meta(),
        "board_name": board.name,
        "model_labels": {
            m: registry.models[m].display
            for m in dict.fromkeys(setup.models)
            if m and m in registry.models and registry.models[m].display
        },
    }


def _setup_from_meta(meta: dict[str, Any]) -> GameSetup:
    s = meta["setup"]
    return GameSetup(
        board=s["board"],
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
    )


def main(argv: list[str]) -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", dest="config", type=Path, default=None, metavar="PATH", help="JSON 配置文件路径")
    args, rest = pre.parse_known_args(argv)
    settings = WerewolfSettings.load(cli_args=rest, config_file=args.config)
    run_async_main(run_game(settings))
