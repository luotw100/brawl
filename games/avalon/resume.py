"""柔性断点续生成：让一局在中途崩了（超时/限流/进程被杀）之后，能从最后一个**完整
节点**接着往下跑，而不是从头重烧。

机制三件套：
  1. LangGraph 的 checkpointer（AsyncSqliteSaver，落在 replay 目录里的 state.sqlite）——
     每个 super-step 提交后持久化整份 GameState（含各席位 append-only 线程），崩溃时最后
     一个完整节点的状态可恢复。
  2. 行数水位 sidecar（_resume.jsonl）——跑图时开 `checkpoints` stream，某 super-step 的
     检查点**就在该步 custom 事件落完之后**到达（实测严格有序），编排层此刻把
     "这一步的 checkpoint_id" 和 "此刻 events/llm_calls 两份产物的行数" 配成一对追加一行。
  3. 续跑时取 sidecar 最后一行：先把两份产物 truncate 回该水位（砍掉断点节点重写了一半的
     脏行），再让图从该 checkpoint_id 续跑。**截断点与续跑点同源于这一对记录**，故二者
     严格对齐——不会留重复行，也不会丢已完成的行。

为什么水位不直接塞进 checkpoint：实测异步 saver 的 aput 是延迟/批量提交的，无法在写
checkpoint 的那一刻可靠地把当时的行数戳进去；也不能事后用 aget_state 反查 checkpoint_id
（它会比当前 super-step 落后一步）。而 `checkpoints` stream 与 custom/values 同序到达，
检查点 chunk 到手时该步事件已记完、该步模型调用也已落盘，读 recorder.counts() 即精确水位。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# 一个 replay 目录 = 一局，故 thread_id 用常量即可（checkpointer 按它归并本局的检查点链）。
THREAD_ID = "avalon"
_SIDECAR = "_resume.jsonl"
_DB = "state.sqlite"


def db_path(run_dir: Path) -> str:
    """本局 checkpoint 数据库路径（AsyncSqliteSaver.from_conn_string 用）。"""
    return str(run_dir / _DB)


def has_checkpoint(run_dir: Path) -> bool:
    """这个 replay 目录里有没有可续跑的 checkpoint 数据库。"""
    return (run_dir / _DB).exists()


@dataclass(frozen=True)
class Watermark:
    """sidecar 里的一对记录：某个完整 super-step 的 checkpoint 与当时两份产物的行数水位。"""

    checkpoint_id: str
    events_n: int
    llm_n: int


def record_watermark(run_dir: Path, mark: Watermark) -> None:
    """在 `values` 边界追加一行水位（append-only）：checkpoint_id ↔ (events, llm_calls)。

    只在 checkpoint 已可查（aget_state 已返回该 id）之后调用，故 sidecar 里的每个 id 都
    必然已持久化、且其行数与该 checkpoint 表示的状态精确对应。"""
    line = json.dumps(
        {"checkpoint_id": mark.checkpoint_id, "events": mark.events_n, "llm_calls": mark.llm_n},
        ensure_ascii=False,
    )
    with (run_dir / _SIDECAR).open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()  # 每步落定即刷盘，崩溃时这一对记录要在


def recorded_ids(run_dir: Path) -> set[str]:
    """sidecar 里已记过的所有 checkpoint_id。续跑时 LangGraph 会先把历史 values 重放一遍
    （那些步的节点并不重跑、不再发 custom 事件），据此跳过、不把历史步重记进 sidecar。"""
    path = run_dir / _SIDECAR
    if not path.exists():
        return set()
    ids = set()
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            ids.add(json.loads(ln)["checkpoint_id"])
    return ids


def last_watermark(run_dir: Path) -> Watermark | None:
    """取 sidecar 最后一行水位；空/不存在返回 None（无可恢复的完整 super-step）。"""
    path = run_dir / _SIDECAR
    if not path.exists():
        return None
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    d = json.loads(lines[-1])
    return Watermark(checkpoint_id=d["checkpoint_id"], events_n=d["events"], llm_n=d["llm_calls"])
