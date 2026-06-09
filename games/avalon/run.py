"""一局的编排：解析校验配置 → 装配 Deps → 建初始席位 → 跑图 → 把事件流喂给渲染器。

整局的"开局设定"（座位→人设、谁拿什么角色、首任队长）在 validation.py 边界一次性
解析+摇定成 GameSetup（缺省即用 registry 默认/rng 摇定），图里拿到的是确定值；
这里拿现成的方案按座位号拼装初始席位，role/intel 入图前就填好——夜晚环节（梅林看红方、
红方互认）是 roles 的纯派生，不另掷骰子。
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime
from pathlib import Path

from avalon import rules
from avalon.console import Renderer
from avalon.deps import Deps
from avalon.graph import build_graph
from avalon.player import build_player
from avalon.recorder import open_recorder
from avalon.registry import Registry, load_registry
from avalon.settings import AvalonSettings
from avalon.state import GameState, Seat
from avalon.validation import GameSetup, resolve_and_validate


def _build_seats(registry: Registry, setup: GameSetup) -> list[Seat]:
    """setup 已由 resolve_and_validate 解析+摇定好，这里只管按座位号（1 起）拼装、把
    role/intel 填进去——夜晚环节（可见性矩阵 rules.night_intel）是 roles 的纯派生，
    不另掷骰子。"""
    seats = []
    for i, role in enumerate(setup.roles):
        no = i + 1
        seats.append(
            Seat(
                no=no, persona_id=setup.cast[i], strategy_id=setup.strategies[i],
                model=setup.models[i], role=role, intel=rules.night_intel(setup.roles, no),
            )
        )
    return seats


async def run_game(settings: AvalonSettings) -> GameState:
    """跑完整一局，边跑边渲染，返回最终状态。"""
    registry = load_registry()
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)  # 边界：解析+摇定整局方案 + 查清所有跨引用约束
    recorder = open_recorder(settings.replay)
    recorder.write_meta({
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "player": settings.player,  # 用哪个后端跑的——推不出来，留着；其余 settings 字段都被 setup 覆盖或可派生
        "setup": setup.to_meta(),  # 这一局的确定性方案：角色/首任队长都已摇定，复盘唯一真源
    })
    deps = Deps(
        settings=settings,
        registry=registry,
        player=build_player(settings.player, rng, recorder=recorder, extractor=registry.models[settings.extractor]),
    )
    seats = _build_seats(registry, setup)
    graph = build_graph()
    renderer = Renderer()

    config = {"configurable": {"deps": deps}, "recursion_limit": 200}
    initial = GameState(
        seats=seats,
        leader=setup.first_leader,
        lady_holder=setup.lady_holder,
        lady_used=[setup.lady_holder] if setup.lady_holder is not None else [],
    )

    final: GameState | None = None
    async for mode, chunk in graph.astream(initial, config=config, stream_mode=["custom", "values"]):
        if mode == "custom":
            # 同一份 god-view 事件扇出两家：渲染器实时打印 + 录制器落 events.jsonl 供回放。
            renderer.handle(chunk)
            recorder.record_event(chunk)
        else:  # "values"：每步后的完整状态，留最后一个作为返回值
            final = GameState(**chunk) if not isinstance(chunk, GameState) else chunk

    return final if final is not None else initial


def main(argv: list[str]) -> None:
    """`brawl avalon` 的极薄入口:剥掉 --config(它是"读哪个文件"的元参数,不是配置字段),
    把剩余 argv 交给 AvalonSettings 自己的 CLI source。所有 --players/--seed 等 flag
    都由 settings 字段自动派生,这里不枚举任何参数。"""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", dest="config", type=Path, default=None, metavar="PATH", help="JSON 配置文件路径")
    args, rest = pre.parse_known_args(argv)
    settings = AvalonSettings.load(cli_args=rest, config_file=args.config)
    asyncio.run(run_game(settings))
