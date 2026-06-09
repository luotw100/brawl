"""录制落盘：把一局的完整产物写进 --replay 指定的目录，供直播回放与复盘。

games 是"对局快照生成器"：跑一遍把一局冻成 replay，直播时回放它而非实时跑。
一个目录 = 一局的完整产物（replay），平铺三份文件（各玩法统一契约）：

    events.jsonl     god-view 事件时间线，驱动回放/画面（与 console.Renderer 同源）
    llm_calls.jsonl  每次模型调用一行（含离线 FakeBackend：它也走完整 LLMPlayer 链路，照常落盘）
    meta.json        本局配置快照 + 起始时间

不给 --replay 时走 NullRecorder（空对象），调用方零分叉、只渲染不落盘、零开销。
"目录已存在即报错——不覆盖、不加 --force，要重跑换个名"这条约束由 validation.py 在
边界把守（本模块默认拿到的就是干净入参）；DirRecorder 的 exist_ok=False 只作纵深防御。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class Recorder(Protocol):
    """一局产物的落盘接口。两种实现：DirRecorder 真写盘，NullRecorder 全空操作。

    调用方无脑调用、不再判断"要不要落盘"——落不落盘是实现自己的事（空对象模式：
    用一个满足同样接口、方法全空的对象替代 None，把分支从所有调用点收进类型里）。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def record_event(self, ev: dict[str, Any]) -> None: ...
    def record_llm_call(self, record: dict[str, Any]) -> None: ...


class DirRecorder:
    """一局=一个目录：平铺写 events.jsonl / llm_calls.jsonl / meta.json。"""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=False)
        self._events_path = run_dir / "events.jsonl"
        self._llm_path = run_dir / "llm_calls.jsonl"
        self._events_path.touch()
        self._llm_path.touch()  # 预创建：即便一局零模型调用（如刚开局即异常）也留个空文件

    def write_meta(self, meta: dict[str, Any]) -> None:
        (self.run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_event(self, ev: dict[str, Any]) -> None:
        with self._events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def record_llm_call(self, record: dict[str, Any]) -> None:
        with self._llm_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class NullRecorder:
    """不落盘：三个方法全是空操作。不给 --replay 时用它，调用方零分叉、零开销。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def record_event(self, ev: dict[str, Any]) -> None: ...
    def record_llm_call(self, record: dict[str, Any]) -> None: ...


def open_recorder(replay: Path | None) -> Recorder:
    """--replay 给了就把这局产物平铺写进该目录；不给返回 NullRecorder（只渲染不落盘）。

    "目录不存在"由 validation 在边界保证；DirRecorder 的 exist_ok=False 只作
    纵深防御（挡 TOCTOU 与直接 new 的误用）。"""
    return DirRecorder(replay) if replay is not None else NullRecorder()
