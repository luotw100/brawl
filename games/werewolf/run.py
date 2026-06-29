"""狼人杀对局入口：解析校验配置 -> 装配依赖 -> 跑图 -> 写 replay。"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from core import resume as resume_mod
from core.recorder import Recorder, open_recorder
from core.runner import drive_graph, graph_config
from werewolf.console import Renderer
from werewolf.deps import Deps
from werewolf.graph import build_graph
from werewolf.player import build_player
from werewolf.registry import Registry, load_registry
from werewolf.settings import WerewolfSettings
from werewolf.state import GameState, Seat
from werewolf.validation import GameSetup, resolve_and_validate, validate_resume

_THREAD_ID = "werewolf"


def _build_seats(setup: GameSetup) -> list[Seat]:
    return [
        Seat(
            no=i + 1,
            persona_id=setup.cast[i],
            strategy_id=setup.strategies[i],
            model=setup.models[i],
            role=role,
        )
        for i, role in enumerate(setup.roles)
    ]


async def run_game(settings: WerewolfSettings) -> GameState:
    registry = load_registry()
    renderer = Renderer()
    if settings.resume:
        return await _resume_game(settings, registry, renderer)
    return await _fresh_game(settings, registry, renderer)


async def _fresh_game(settings: WerewolfSettings, registry: Registry, renderer: Renderer) -> GameState:
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    recorder = open_recorder(settings.replay)
    recorder.write_meta(
        {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "game": "werewolf",
            "player": settings.player,
            "setup": setup.to_meta(),
            "model_labels": {
                m: registry.models[m].display
                for m in dict.fromkeys(setup.models)
                if m in registry.models and registry.models[m].display
            },
        }
    )
    deps = _make_deps(settings, registry, recorder, rng)
    initial = GameState(seats=_build_seats(setup), board=setup.board.id)
    for seat in initial.seats:
        src = registry.avatar_path(seat.model)
        if src is not None:
            recorder.write_avatar(seat.no, src)
    if settings.replay is None:
        return await _drive_werewolf(build_graph(), initial, deps, recorder, renderer, run_dir=None)
    async with AsyncSqliteSaver.from_conn_string(resume_mod.db_path(settings.replay)) as saver:
        graph = build_graph(checkpointer=saver)
        return await _drive_werewolf(graph, initial, deps, recorder, renderer, run_dir=settings.replay)


async def _resume_game(settings: WerewolfSettings, registry: Registry, renderer: Renderer) -> GameState:
    validate_resume(settings, registry)
    run_dir = settings.replay
    assert run_dir is not None
    wm = resume_mod.last_watermark(run_dir)
    assert wm is not None
    recorder = open_recorder(run_dir, resume=True)
    recorder.truncate(wm.events_n, wm.llm_n)
    deps = _make_deps(settings, registry, recorder, random.Random(settings.seed))
    async with AsyncSqliteSaver.from_conn_string(resume_mod.db_path(run_dir)) as saver:
        graph = build_graph(checkpointer=saver)
        config = graph_config(deps, thread_id=_THREAD_ID, checkpoint_id=wm.checkpoint_id)
        return await drive_graph(graph, None, config, recorder, renderer, run_dir, GameState)


async def _drive_werewolf(graph, stream_input, deps: Deps, recorder: Recorder, renderer: Renderer, run_dir: Path | None) -> GameState:
    config = graph_config(deps, thread_id=_THREAD_ID)
    return await drive_graph(graph, stream_input, config, recorder, renderer, run_dir, GameState)


def _make_deps(settings: WerewolfSettings, registry: Registry, recorder: Recorder, rng: random.Random) -> Deps:
    return Deps(
        settings=settings,
        registry=registry,
        player=build_player(settings.player, rng, recorder=recorder, extractor=registry.models[settings.extractor]),
    )


def main(argv: list[str]) -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", dest="config", type=Path, default=None, metavar="PATH", help="JSON 配置文件路径")
    args, rest = pre.parse_known_args(argv)
    settings = WerewolfSettings.load(cli_args=rest, config_file=args.config)
    asyncio.run(run_game(settings))

