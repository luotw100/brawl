"""句子日志 = 一局的真相与 checkpoint。

一个 expectation（turn）的玩家句子与其前后 host 串场合批一次 write+fsync 落盘——
崩溃只可能丢整个 turn，torn 超步不存在。没有旧引擎那套 sidecar / watermark /
"custom 事件严格先于 checkpoint chunk"的两路流对齐——只有一路 sentences。

`--replay` 给了就落盘；不给时走 NullJournal（空对象），调用方零分叉。
"""

from __future__ import annotations

import json
import os
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
    def append_batch(self, sents: list[Any]) -> int: ...
    def save_snapshot(self, seq: int, state_bytes: bytes) -> None: ...
    def latest_snapshot(self) -> tuple[int, bytes] | None: ...
    def load_sentences(self) -> list[dict[str, Any]]: ...
    def close(self) -> None: ...
    def next_seq(self) -> int: ...


class DirJournal:
    """一局 = 一个目录：sentences.jsonl + 可选快照。

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

    def append_batch(self, sents: list[Any]) -> int:
        """原子记录一批 Sentence（一个 turn 的玩家句子 + 其前后 host 串场）。

        seq 由本方法按水位顺次赋给（写入 sent.seq）；整批一次 write + 一次 fsync——
        要么全落要么全不落（进程死在中途最多留个半行，load 时按 torn 尾行丢弃）。
        返回本批最后一条的 seq；空批不调用（调用方保证）。
        """
        lines = []
        last = -1
        for sent in sents:
            sent.seq = self.next_seq()
            last = sent.seq
            lines.append(json.dumps(sent.model_dump(), ensure_ascii=False))
        self._sent_fp.write("\n".join(lines) + "\n")
        self._sent_fp.flush()
        os.fsync(self._sent_fp.fileno())
        return last

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
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return d["seq"], base64.b64decode(d["state"])
        except (json.JSONDecodeError, KeyError, ValueError):
            return None  # 快照写到一半崩了：按无快照处理，全量重放兜底（正确性不靠它）

    def load_sentences(self) -> list[dict[str, Any]]:
        """读出全部句子。崩溃可能留半行的 torn 尾行——只容忍丢弃最后一行；
        中段坏行是真损坏，照样抛 JSONDecodeError（fail-fast 不吞数据）。"""
        if not self._sent_path.exists():
            return []
        lines = [ln for ln in self._sent_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        rows: list[dict[str, Any]] = []
        for i, ln in enumerate(lines):
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    break  # torn 尾行：丢弃，这个 turn 由续跑重问
                raise
        return rows

    def close(self) -> None:
        self._sent_fp.close()


class NullJournal:
    """不落盘：方法全是空操作。不给 --replay 时用它，调用方零分叉、零开销。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...
    def next_seq(self) -> int:
        return 0

    def append_batch(self, sents: list[Any]) -> int:
        return -1

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
