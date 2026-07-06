"""录制落盘：把一局的完整产物写进 --replay 指定的目录，供直播回放与复盘。

games 是"对局快照生成器"：跑一遍把一局冻成 replay，直播时回放它而非实时跑。
一个目录 = 一局的完整产物（replay），平铺三份文件（各玩法统一契约）：

    events.jsonl     god-view 事件时间线，驱动回放/画面（与 console.Renderer 同源）
    llm_calls.jsonl  每次 agent 调用一行（含离线 RandomAgent：它也走完整 AgentPlayer 链路，照常落盘）
    meta.json        本局配置快照 + 起始时间
    avatars/<座位号>.png  各座位厂商 logo（开局一次性烧入，缺省=该座位无头像，渲染回退 hash 色）

不给 --replay 时走 NullRecorder（空对象），调用方零分叉、只渲染不落盘、零开销。
"目录已存在即报错——不覆盖、不加 --force，要重跑换个名"这条约束由 validation.py 在
边界把守（本模块默认拿到的就是干净入参）；DirRecorder 的 exist_ok=False 只作纵深防御。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Protocol


class Recorder(Protocol):
    """一局产物的落盘接口。两种实现：DirRecorder 真写盘，NullRecorder 全空操作。

    调用方无脑调用、不再判断"要不要落盘"——落不落盘是实现自己的事（空对象模式：
    用一个满足同样接口、方法全空的对象替代 None，把分支从所有调用点收进类型里）。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...
    def record_event(self, ev: dict[str, Any]) -> None: ...
    def record_llm_call(self, record: dict[str, Any]) -> None: ...
    def counts(self) -> tuple[int, int]: ...
    def truncate(self, events_n: int, llm_n: int) -> None: ...


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as f:
        return sum(1 for _ in f)


def _truncate_lines(path: Path, n: int) -> None:
    """把文件截到只剩前 n 行（多出来的尾巴丢掉）。用于续跑时砍掉断点节点写了一半的脏行。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("".join(line + "\n" for line in lines[:n]), encoding="utf-8")


class DirRecorder:
    """一局=一个目录：平铺写 events.jsonl / llm_calls.jsonl / meta.json。

    维护两份文件的行数水位（counts）：续跑时由编排层把它和 checkpoint 配成一对存进
    sidecar，恢复时据此 truncate 掉断点节点重写的脏行（见 resume.py）。"""

    def __init__(self, run_dir: Path, resume: bool = False) -> None:
        self.run_dir = run_dir
        self._events_path = run_dir / "events.jsonl"
        self._llm_path = run_dir / "llm_calls.jsonl"
        if resume:
            # 续跑：目录与产物已在（validation 边界保证），不重建、不清空，按现有行数续接水位。
            self._events_n = _line_count(self._events_path)
            self._llm_n = _line_count(self._llm_path)
        else:
            run_dir.mkdir(parents=True, exist_ok=False)
            self._events_path.touch()
            self._llm_path.touch()  # 预创建：即便一局零模型调用（如刚开局即异常）也留个空文件
            self._events_n = 0
            self._llm_n = 0

    def write_meta(self, meta: dict[str, Any]) -> None:
        (self.run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_avatar(self, seat_no: int, src: Path) -> None:
        """把座位 seat_no 的厂商 logo 源文件拷进 avatars/<seat_no><后缀>。开局一次性写，
        回放渲染层据「该座位有没有头像文件」决定显真实头像还是 hash 色回退。"""
        avatars = self.run_dir / "avatars"
        avatars.mkdir(exist_ok=True)
        shutil.copyfile(src, avatars / f"{seat_no}{src.suffix}")

    def record_event(self, ev: dict[str, Any]) -> None:
        with self._events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._events_n += 1

    def record_llm_call(self, record: dict[str, Any]) -> None:
        with self._llm_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._llm_n += 1

    def counts(self) -> tuple[int, int]:
        """当前两份产物的行数水位 (events, llm_calls)——已落盘的行数，续跑对齐用。"""
        return (self._events_n, self._llm_n)

    def truncate(self, events_n: int, llm_n: int) -> None:
        """把两份产物砍回指定水位（续跑恢复时调一次），随后续写从该水位接上。"""
        _truncate_lines(self._events_path, events_n)
        _truncate_lines(self._llm_path, llm_n)
        self._events_n = events_n
        self._llm_n = llm_n


class NullRecorder:
    """不落盘：方法全是空操作。不给 --replay 时用它，调用方零分叉、零开销。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...
    def record_event(self, ev: dict[str, Any]) -> None: ...
    def record_llm_call(self, record: dict[str, Any]) -> None: ...
    def counts(self) -> tuple[int, int]:
        return (0, 0)
    def truncate(self, events_n: int, llm_n: int) -> None: ...


def open_recorder(replay: Path | None, resume: bool = False) -> Recorder:
    """--replay 给了就把这局产物平铺写进该目录；不给返回 NullRecorder（只渲染不落盘）。

    resume=True 时按"续跑"打开既有目录（不重建、不清空，承接现有行数水位）；新局
    （resume=False）目录不存在由 validation 在边界保证，DirRecorder 的 exist_ok=False
    只作纵深防御（挡 TOCTOU 与直接 new 的误用）。"""
    return DirRecorder(replay, resume=resume) if replay is not None else NullRecorder()
