"""一局的编排：解析校验配置 → 装配 Deps → 建初始席位 → 跑图 → 把事件流喂给渲染器。

整局的"开局设定"（座位→人设、谁拿什么角色、首任队长）在 validation.py 边界一次性
解析+摇定成 GameSetup（缺省即用 registry 默认/rng 摇定），图里拿到的是确定值；
这里拿现成的方案按座位号拼装初始席位，role/intel 入图前就填好——夜晚环节（梅林看红方、
红方互认）是 roles 的纯派生，不另掷骰子。

给了 --replay 时，跑图挂一个 LangGraph checkpointer（落 replay/state.sqlite），每个
super-step 后持久化整份 GameState，并在 `values` 边界把 (checkpoint_id, 产物行数) 配对
追加进 sidecar；中途崩了用 --resume 从最后一个完整 super-step 接着跑（见 resume.py）。
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from avalon import resume as resume_mod
from avalon import rules
from avalon.console import Renderer
from avalon.deps import Deps
from avalon.graph import build_graph
from avalon.player import build_player
from avalon.recorder import Recorder, open_recorder
from avalon.registry import Registry, load_registry
from avalon.settings import AvalonSettings
from avalon.state import GameState, Seat
from avalon.validation import GameSetup, resolve_and_validate, validate_resume


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


def _config(deps: Deps, checkpoint_id: str | None = None) -> dict:
    """跑图的 config：deps 走 configurable（运行时注入、不入 checkpoint），thread_id 让
    checkpointer 归并本局检查点链；续跑时再钉上 checkpoint_id 从那一步接着跑。"""
    configurable: dict = {"deps": deps, "thread_id": resume_mod.THREAD_ID}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable, "recursion_limit": 200}


async def _drive(
    graph, stream_input, config: dict, recorder: Recorder, renderer: Renderer, run_dir: Path | None,
) -> GameState:
    """跑图主循环。三路 stream 同序到达，给了 --replay（run_dir 非空）时据此落 sidecar：
      - custom：god-view 事件，扇出渲染 + 落 events.jsonl；
      - checkpoints：某 super-step 的检查点**就在该步 custom 事件落完之后**到达（实测严格
        有序），此刻 recorder.counts() 恰是"截至该步"的产物行数——把 (checkpoint_id, 行数)
        配对追加进 sidecar，这一对就是续跑时的"截断点 + 续跑点"，同源故严格对齐；
      - values：每步后的完整状态，留最后一个作返回值。
    run_dir=None（无 --replay）时只渲染、不挂 checkpointer/sidecar。"""
    seen = resume_mod.recorded_ids(run_dir) if run_dir is not None else set()
    final: GameState | None = None
    async for mode, chunk in graph.astream(stream_input, config=config, stream_mode=["custom", "values", "checkpoints"]):
        if mode == "custom":
            # 同一份 god-view 事件扇出两家：渲染器实时打印 + 录制器落 events.jsonl 供回放。
            renderer.handle(chunk)
            recorder.record_event(chunk)
        elif mode == "checkpoints" and run_dir is not None:
            cid = chunk["config"]["configurable"]["checkpoint_id"]
            if cid not in seen:  # 跳过续跑时被重放的历史步（节点不重跑、产物不增长）
                seen.add(cid)
                e, n = recorder.counts()
                resume_mod.record_watermark(run_dir, resume_mod.Watermark(cid, e, n))
        elif mode == "values":
            final = GameState(**chunk) if not isinstance(chunk, GameState) else chunk
    assert final is not None  # astream 至少 yield 一次初始 values
    return final


async def run_game(settings: AvalonSettings) -> GameState:
    """跑完整一局，边跑边渲染，返回最终状态。--resume 则从既有对局的断点接着跑。"""
    registry = load_registry()
    renderer = Renderer()
    if settings.resume:
        return await _resume_game(settings, registry, renderer)
    return await _fresh_game(settings, registry, renderer)


async def _fresh_game(settings: AvalonSettings, registry: Registry, renderer: Renderer) -> GameState:
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)  # 边界：解析+摇定整局方案 + 查清所有跨引用约束
    recorder = open_recorder(settings.replay)
    recorder.write_meta({
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "player": settings.player,  # 用哪个后端跑的——推不出来，留着；其余 settings 字段都被 setup 覆盖或可派生
        "setup": setup.to_meta(),  # 这一局的确定性方案：角色/首任队长都已摇定，复盘唯一真源
        # 逻辑名→对外展示名：烧进 meta 让回放自包含（stage 据座位 model 查这表显在头像下，
        # 与 names 同套路）。缺 display 的模型不收，stage 那边查不到就不显。
        "model_labels": {
            m: registry.models[m].display
            for m in dict.fromkeys(setup.models)  # 去重保序
            if m and m in registry.models and registry.models[m].display
        },
    })
    deps = _make_deps(settings, registry, recorder, rng)
    initial = GameState(
        seats=_build_seats(registry, setup),
        leader=setup.first_leader,
        lady_holder=setup.lady_holder,
        lady_used=[setup.lady_holder] if setup.lady_holder is not None else [],
    )
    for seat in initial.seats:  # 开局一次性烧入各座位厂商 logo（无 --replay 时 NullRecorder 空操作）
        src = registry.avatar_path(seat.model)
        if src is not None:  # 该模型没配 avatar / 源文件缺失 → 不烧入，回放渲染走 hash 色回退
            recorder.write_avatar(seat.no, src)
    if settings.replay is None:  # 无产物目录=不可续：纯内存跑，不挂 checkpointer/sidecar
        return await _drive(build_graph(), initial, _config(deps), recorder, renderer, run_dir=None)
    async with AsyncSqliteSaver.from_conn_string(resume_mod.db_path(settings.replay)) as saver:
        graph = build_graph(checkpointer=saver)
        return await _drive(graph, initial, _config(deps), recorder, renderer, run_dir=settings.replay)


async def _resume_game(settings: AvalonSettings, registry: Registry, renderer: Renderer) -> GameState:
    """从断点续跑：开局方案与对局状态都从 meta/checkpoint 恢复（不重摇），先把产物砍回
    最后一个完整 super-step 的水位（丢掉断点节点写了一半的脏行），再让图从该 checkpoint 续跑。"""
    validate_resume(settings, registry)  # 边界：目录在、有 checkpoint 与可续水位、模型 key 都齐
    run_dir = settings.replay
    assert run_dir is not None  # settings._check 已保证 --resume 必带 --replay
    wm = resume_mod.last_watermark(run_dir)
    assert wm is not None  # validate_resume 已保证有可续水位
    recorder = open_recorder(run_dir, resume=True)
    recorder.truncate(wm.events_n, wm.llm_n)  # 砍掉断点节点重写的脏行，水位归到 checkpoint 那一刻
    deps = _make_deps(settings, registry, recorder, random.Random(settings.seed))
    async with AsyncSqliteSaver.from_conn_string(resume_mod.db_path(run_dir)) as saver:
        graph = build_graph(checkpointer=saver)
        config = _config(deps, checkpoint_id=wm.checkpoint_id)
        return await _drive(graph, None, config, recorder, renderer, run_dir=run_dir)


def _make_deps(settings: AvalonSettings, registry: Registry, recorder: Recorder, rng: random.Random) -> Deps:
    return Deps(
        settings=settings,
        registry=registry,
        player=build_player(settings.player, rng, recorder=recorder, extractor=registry.models[settings.extractor]),
    )


def main(argv: list[str]) -> None:
    """`brawl avalon` 的极薄入口:剥掉 --config(它是"读哪个文件"的元参数,不是配置字段),
    把剩余 argv 交给 AvalonSettings 自己的 CLI source。所有 --players/--seed 等 flag
    都由 settings 字段自动派生,这里不枚举任何参数。"""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", dest="config", type=Path, default=None, metavar="PATH", help="JSON 配置文件路径")
    args, rest = pre.parse_known_args(argv)
    settings = AvalonSettings.load(cli_args=rest, config_file=args.config)
    asyncio.run(run_game(settings))
