"""句子日志 = 一局的真相与 checkpoint。

一行 Sentence，写盘（fsync）即推进，就是一次原子 checkpoint。没有旧引擎那套
sidecar / watermark / "custom 事件严格先于 checkpoint chunk"的两路流对齐——只有
一路 sentences，torn 超步不存在（fsync 后才递进 seq）。

`--replay` 给了就落盘；不给时走 NullJournal（空对象），调用方零分叉。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Protocol

_SENTENCES = "sentences.jsonl"
_SNAPSHOT = "_snapshot.json"
_META = "meta.json"


class Journal(Protocol):
    """一局产物落盘接口。DirJournal 真写，NullJournal 全空操作（空对象模式）。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...
    def append_sentence(self, sent: Any) -> None: ...
    def save_snapshot(self, seq: int, state_bytes: bytes) -> None: ...
    def latest_snapshot(self) -> tuple[int, bytes] | None: ...
    def load_sentences(self) -> list[dict[str, Any]]: ...
    def close(self) -> None: ...
    def next_seq(self) -> int: ...


class DirJournal:
    """一局 = 一个目录：sentences.jsonl + llm_calls.jsonl + 可选快照。

    resume=True 时按既有行数水位续接（不重建、不清空）；新局（resume=False）目录不存在
    由玩法 validation 在边界保证，这里 exist_ok=False 只作纵深防御。
    """

    def __init__(self, run_dir: Path, *, resume: bool = False) -> None:
        self.run_dir = run_dir
        self._sent_path = run_dir / _SENTENCES
        if resume:
            self._sent_path.touch(exist_ok=True)
        else:
            run_dir.mkdir(parents=True, exist_ok=False)
            self._sent_path.touch()
        self._sent_fp = self._sent_path.open("a", encoding="utf-8")
        # 续跑：以已有句子序号水位续接，避免重复写
        self._next_seq = self._existing_seq_count()

    def _existing_seq_count(self) -> int:
        if not self._sent_path.exists():
            return 0
        with self._sent_path.open("rb") as f:
            return sum(1 for _ in f)

    def write_meta(self, meta: dict[str, Any]) -> None:
        (self.run_dir / _META).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_avatar(self, seat_no: int, src: Path) -> None:
        avatars = self.run_dir / "avatars"
        avatars.mkdir(exist_ok=True)
        shutil.copyfile(src, avatars / f"{seat_no}{src.suffix}")

    def next_seq(self) -> int:
        s = self._next_seq
        self._next_seq += 1
        return s

    def append_sentence(self, sent: Any) -> None:
        """原子记录一条 Sentence。写盘并 fsync 后才算完成一次超推进。"""
        self._sent_fp.write(json.dumps(sent.model_dump(), ensure_ascii=False) + "\n")
        self._sent_fp.flush()
        import os

        os.fsync(self._sent_fp.fileno())

    def save_snapshot(self, seq: int, state_bytes: bytes) -> None:
        """语意：'seq 及之前所有 sentences 已 apply 进这份 state'。加速 resume 用。

        正确性不依赖它——删掉它，靠从头重放 sentences 也能恢复。节流策略由 loop 决定。
        存为单文件覆盖式（最新一份够用）；对局很长时可考虑滚动，但 KISS 先做最简。
        """
        import base64

        payload = {"seq": seq, "state": base64.b64encode(state_bytes).decode("ascii")}
        (self.run_dir / _SNAPSHOT).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def latest_snapshot(self) -> tuple[int, bytes] | None:
        import base64

        path = self.run_dir / _SNAPSHOT
        if not path.exists():
            return None
        d = json.loads(path.read_text(encoding="utf-8"))
        return d["seq"], base64.b64decode(d["state"])

    def load_sentences(self) -> list[dict[str, Any]]:
        if not self._sent_path.exists():
            return []
        lines = self._sent_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(ln) for ln in lines if ln.strip()]

    def close(self) -> None:
        self._sent_fp.close()


class NullJournal:
    """不落盘：方法全是空操作。不给 --replay 时用它，调用方零分叉、零开销。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...
    def next_seq(self) -> int:
        return 0

    def append_sentence(self, sent: Any) -> None: ...
    def save_snapshot(self, seq: int, state_bytes: bytes) -> None: ...
    def latest_snapshot(self) -> tuple[int, bytes] | None:
        return None

    def load_sentences(self) -> list[dict[str, Any]]:
        return []

    def close(self) -> None: ...


def open_journal(replay: Path | None, *, resume: bool = False) -> Journal:
    """--replay 给了就写盘该目录；不给返回 NullJournal。玩法 validation 在边界保证
    新局目录不存在 / 续跑目录存在，这里只作纵深防御。"""
    return DirJournal(replay, resume=resume) if replay is not None else NullJournal()
