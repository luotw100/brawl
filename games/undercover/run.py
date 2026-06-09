"""一局的编排：解析校验配置 → 装配 Deps → 建初始席位 → 跑图 → 把事件流喂给渲染器。

整局的"开局设定"（座位→人设、座位→模型、谁是卧底、用哪对词）在 validation.py 边界
一次性解析+摇定成 GameSetup（缺省即用 registry 默认/rng 摇定），图里拿到的是确定值；
这里拿现成的方案按座位号拼装初始席位，role/word 入图前就填好。人设与模型解耦——同一
人设可挂任意模型。
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime
from pathlib import Path

from undercover.console import Renderer
from undercover.deps import Deps
from undercover.graph import build_graph
from undercover.player import build_player
from undercover.recorder import open_recorder
from undercover.registry import Registry, load_registry
from undercover.settings import UndercoverSettings
from undercover.state import GameState, Role, Seat
from undercover.validation import GameSetup, resolve_and_validate


def _build_seats(settings: UndercoverSettings, registry: Registry, setup: GameSetup) -> list[Seat]:
    """setup 已由 resolve_and_validate 解析+摇定好，这里只管按座位号拼装、把 role/word 填进去。"""
    civ_word, undercover_word = setup.word_pair
    seats = []
    for no in range(settings.players):
        persona = registry.personas[setup.cast[no]]
        role = setup.roles[no]
        word = undercover_word if role is Role.UNDERCOVER else civ_word
        seats.append(Seat(no=no, persona_id=persona.id, strategy_id=setup.strategies[no], name=persona.name, model=setup.models[no], role=role, word=word))
    return seats


async def run_game(settings: UndercoverSettings) -> GameState:
    """跑完整一局，边跑边渲染，返回最终状态。"""
    registry = load_registry()
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)  # 边界：解析+摇定整局方案 + 查清所有跨引用约束
    recorder = open_recorder(settings.replay)
    recorder.write_meta({
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "player": settings.player,  # 用哪个后端跑的（llm/random）——推不出来，留着；其余 settings 字段都被 setup 覆盖或可派生
        "setup": setup.to_meta(),  # 这一局的确定性方案：随机词/随机身份都已摇定，复盘唯一真源
    })
    deps = Deps(
        settings=settings,
        registry=registry,
        player=build_player(settings.player, registry, rng, recorder=recorder),
        rng=rng,
    )
    seats = _build_seats(settings, registry, setup)
    graph = build_graph()
    renderer = Renderer()

    config = {"configurable": {"deps": deps}, "recursion_limit": 200}
    initial = GameState(seats=seats)

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
    """`brawl undercover` 的极薄入口:剥掉 --config(它是"读哪个文件"的元参数,
    不是配置字段),把剩余 argv 交给 UndercoverSettings 自己的 CLI source。所有
    --players/--timers.deal 等 flag 都由 settings 字段自动派生,这里不枚举任何参数。"""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", dest="config", type=Path, default=None, metavar="PATH", help="JSON 配置文件路径")
    args, rest = pre.parse_known_args(argv)
    settings = UndercoverSettings.load(cli_args=rest, config_file=args.config)
    asyncio.run(run_game(settings))
