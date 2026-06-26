"""LangGraph 对局驱动。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core import resume
from core.recorder import Recorder


def graph_config(deps: Any, *, thread_id: str, checkpoint_id: str | None = None, recursion_limit: int = 200) -> dict:
    """跑图 config：deps 运行时注入，不入 checkpoint。"""
    configurable: dict[str, Any] = {"deps": deps, "thread_id": thread_id}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable, "recursion_limit": recursion_limit}


async def drive_graph(
    graph,
    stream_input,
    config: dict,
    recorder: Recorder,
    renderer,
    run_dir: Path | None,
    state_cls: type,
):
    """消费 LangGraph custom/values/checkpoints 流，扇出事件并维护续跑水位。"""
    seen = resume.recorded_ids(run_dir) if run_dir is not None else set()
    final = None
    async for mode, chunk in graph.astream(stream_input, config=config, stream_mode=["custom", "values", "checkpoints"]):
        if mode == "custom":
            renderer.handle(chunk)
            recorder.record_event(chunk)
        elif mode == "checkpoints" and run_dir is not None:
            cid = chunk["config"]["configurable"]["checkpoint_id"]
            if cid not in seen:
                seen.add(cid)
                e, n = recorder.counts()
                resume.record_watermark(run_dir, resume.Watermark(cid, e, n))
        elif mode == "values":
            final = state_cls(**chunk) if not isinstance(chunk, state_cls) else chunk
    assert final is not None
    return final
