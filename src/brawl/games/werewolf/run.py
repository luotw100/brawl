"""Werewolf production assembly for fresh games and replay-only resume."""

from __future__ import annotations

import argparse
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from brawl.games.werewolf.registry import Registry, load_registry
from brawl.games.werewolf.rules_engine import WerewolfRules, WerewolfState
from brawl.games.werewolf.settings import WerewolfResumeSettings, WerewolfSettings
from brawl.games.werewolf.validation import (
    GameSetup,
    ReplayMeta,
    build_replay_meta,
    load_replay_meta,
    resolve_and_validate,
)
from brawl.runtime.actor import OpenCodeActor, RandomActor
from brawl.runtime.console import ConsoleRenderer, InstantTypist
from brawl.runtime.journal import Journal, open_journal
from brawl.runtime.loop import resume, run, run_async_main
from brawl.runtime.opencode_backend import OpenCodeBackend
from brawl.runtime.opencode_runtime import (
    OpenCodeInstallation,
    open_opencode_runtime,
    prepare_opencode_installation,
)
from brawl.runtime.threads import SeatThread


async def run_game(settings: WerewolfSettings) -> WerewolfState:
    """新建并跑完一局。fresh settings 是唯一允许指定玩法参数的边界。"""
    registry = load_registry()
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    meta = build_replay_meta(
        settings,
        registry,
        setup,
        started_at=datetime.now(UTC),
    )
    installation = _prepare_runtime(meta.player)
    journal = open_journal(settings.replay, resume=False)
    try:
        journal.write_meta(meta.model_dump(mode="json"))
        _write_avatars(journal, registry, setup)
        rules = WerewolfRules()
        renderer = _build_renderer(meta)
        async with _open_actors(
            meta.player,
            settings.replay,
            setup,
            installation,
            rules,
        ) as actors:
            threads = _new_threads(setup)
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


async def resume_game(settings: WerewolfResumeSettings) -> WerewolfState:
    """仅依 replay 冻结元信息续跑；不加载任何活的玩法 registry。"""
    meta = load_replay_meta(settings.replay)
    setup = meta.setup
    rules = WerewolfRules()
    journal = open_journal(settings.replay, resume=True)
    threads = _new_threads(setup)
    try:
        restored = await resume(
            rules,
            journal=journal,
            threads=threads,
            setup=setup,
        )
        if rules.is_terminal(restored):
            return restored

        installation = _prepare_runtime(meta.player)
        renderer = _build_renderer(meta)
        async with _open_actors(
            meta.player,
            settings.replay,
            setup,
            installation,
            rules,
        ) as actors:
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


@asynccontextmanager
async def _open_actors(
    player: Literal["random", "opencode"],
    replay: Path | None,
    setup: GameSetup,
    installation: OpenCodeInstallation | None,
    rules: WerewolfRules,
) -> AsyncIterator[dict[int, Any]]:
    if player == "random":
        yield {
            no: RandomActor(rules.random_action)
            for no in range(1, len(setup.roles) + 1)
        }
        return

    assert installation is not None and replay is not None
    async with open_opencode_runtime(installation, replay, len(setup.roles)) as connection:
        seat_models = _build_seat_models(setup)
        backend = OpenCodeBackend(connection=connection, seat_models=seat_models)
        await backend.assert_models_available(seat_models.values())
        try:
            yield {
                no: OpenCodeActor(backend)
                for no in range(1, len(setup.roles) + 1)
            }
        finally:
            await backend.close()


def _build_seat_models(setup: GameSetup) -> dict[int, str]:
    return {
        seat: ref
        for seat, ref in enumerate(setup.opencode_models, start=1)
        if ref
    }


def _build_renderer(meta: ReplayMeta) -> ConsoleRenderer | None:
    if not meta.render:
        return None
    return ConsoleRenderer(
        typist=InstantTypist(),
        meta=meta.model_dump(mode="json"),
    )


def _prepare_runtime(player: Literal["random", "opencode"]) -> OpenCodeInstallation | None:
    return prepare_opencode_installation() if player == "opencode" else None


def _new_threads(setup: GameSetup) -> dict[int, SeatThread]:
    return {
        no: SeatThread()
        for no in range(1, len(setup.roles) + 1)
    }


def _write_avatars(journal: Journal, registry: Registry, setup: GameSetup) -> None:
    for seat, model_name in enumerate(setup.models, start=1):
        src = registry.avatar_path(model_name)
        if src is not None:
            journal.write_avatar(seat, src)


def main(argv: list[str]) -> None:
    if "--resume" in argv:
        run_async_main(resume_game(WerewolfResumeSettings.from_cli(argv)))
        return

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(
        "--config",
        dest="config",
        type=Path,
        default=None,
        metavar="PATH",
        help="JSON 配置文件路径",
    )
    args, rest = pre.parse_known_args(argv)
    settings = WerewolfSettings.load(cli_args=rest, config_file=args.config)
    run_async_main(run_game(settings))
