"""Avalon production 入口：开一局与续跑。CLI 路由在 brawl.games.run（板子注册表）。"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brawl.games.avalon.agents import trusted_config
from brawl.games.avalon.registry import Registry, load_registry
from brawl.games.avalon.rules_engine import AvalonRules, AvalonState
from brawl.games.avalon.validation import (
    AvalonReplayMeta,
    GameSetup,
    load_replay,
    resolve_and_validate,
)
from brawl.games.run import RunSettings
from brawl.runtime.actor import OpenCodeActor, RandomActor
from brawl.runtime.console import ConsoleRenderer, InstantTypist
from brawl.runtime.journal import Journal, open_journal
from brawl.runtime.loop import resume, run
from brawl.runtime.opencode_backend import OpenCodeBackend
from brawl.runtime.opencode_runtime import (
    OpenCodeInstallation,
    open_opencode_runtime,
    prepare_opencode_installation,
)
from brawl.runtime.threads import SeatThread


async def run_game(settings: RunSettings) -> AvalonState:
    """按新局 settings 创建 replay；本入口从不恢复既有目录。"""
    registry = load_registry()
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    meta = _build_meta(registry, setup, settings)
    installation = _prepare_runtime(meta)
    journal = open_journal(settings.replay, resume=False)
    try:
        journal.write_meta(meta.model_dump(mode="json"))
        _write_avatars(journal, registry, setup)
        rules = AvalonRules()
        renderer = _build_renderer(meta)
        threads = _threads(setup)
        async with _open_actors(meta, settings.replay, installation, rules) as actors:
            return await run(
                rules,
                setup,
                actors,
                journal,
                threads,
                renderer=renderer,
                max_concurrency=meta.concurrency,
            )
    finally:
        journal.close()


async def resume_game(replay: Path) -> AvalonState:
    """仅依据 replay meta 恢复；不会读取 settings、registry 或任何活 YAML。"""
    meta = load_replay(replay)
    journal = open_journal(replay, resume=True)
    try:
        rules = AvalonRules()
        threads = _threads(meta.setup)
        restored = await resume(rules, journal=journal, threads=threads, setup=meta.setup)
        if rules.is_terminal(restored):
            return restored

        installation = _prepare_runtime(meta)
        renderer = _build_renderer(meta)
        async with _open_actors(meta, replay, installation, rules) as actors:
            return await run(
                rules,
                meta.setup,
                actors,
                journal,
                threads,
                renderer=renderer,
                max_concurrency=meta.concurrency,
            )
    finally:
        journal.close()


@asynccontextmanager
async def _open_actors(
    meta: AvalonReplayMeta,
    replay: Path,
    installation: OpenCodeInstallation | None,
    rules: AvalonRules,
) -> AsyncIterator[dict[int, Any]]:
    if meta.backend == "random":
        yield {
            no: RandomActor(rules.random_action)
            for no in range(1, meta.setup.players + 1)
        }
        return

    assert installation is not None
    if meta.setup.agent_protocol >= 4:
        bundle = meta.agent_config
    elif meta.setup.agent_protocol >= 2:
        bundle = trusted_config(meta.setup.agent_protocol)
    else:
        bundle = None
    async with open_opencode_runtime(
        installation,
        replay,
        meta.setup.players,
        trusted_config=bundle,
    ) as connection:
        seat_models = {
            no: ref
            for no, ref in enumerate(meta.setup.opencode_models, start=1)
            if ref
        }
        backend = OpenCodeBackend(connection=connection, seat_models=seat_models)
        await backend.assert_models_available(seat_models.values())
        try:
            yield {
                no: OpenCodeActor(backend)
                for no in range(1, meta.setup.players + 1)
            }
        finally:
            await backend.close()


def _build_meta(
    registry: Registry,
    setup: GameSetup,
    settings: RunSettings,
) -> AvalonReplayMeta:
    return AvalonReplayMeta(
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        backend=settings.backend,
        concurrency=settings.concurrency,
        render=settings.render,
        seed=settings.seed,
        setup=setup,
        model_labels={
            name: registry.models[name].display
            for name in dict.fromkeys(setup.models)
            if registry.models[name].display
        },
        agent_config=(
            trusted_config(setup.agent_protocol)
            if setup.agent_protocol >= 4
            else None
        ),
    )


def _write_avatars(journal: Journal, registry: Registry, setup: GameSetup) -> None:
    for no, model in enumerate(setup.models, start=1):
        source = registry.avatar_path(model)
        if source is not None:
            journal.write_avatar(no, source)


def _build_renderer(meta: AvalonReplayMeta) -> ConsoleRenderer | None:
    if not meta.render:
        return None
    return ConsoleRenderer(
        typist=InstantTypist(),
        meta=meta.model_dump(mode="json"),
    )


def _prepare_runtime(meta: AvalonReplayMeta) -> OpenCodeInstallation | None:
    return prepare_opencode_installation() if meta.backend == "opencode" else None


def _threads(setup: GameSetup) -> dict[int, SeatThread]:
    return {
        no: SeatThread()
        for no in range(1, setup.players + 1)
    }
