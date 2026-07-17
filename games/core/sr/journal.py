"""句子日志 = 一局的真相与 checkpoint。

磁盘上仍保持 ``sentences.jsonl`` 一行一句，但 checkpoint 单位是完整
turn：一批玩家句子与其 host 串场共享 ``commit_id``。提交时在同目录写完
整新文件、fsync，再 ``os.replace`` 替换旧文件并 fsync 目录。因此崩溃后主文件
只可能是提交前或提交后的完整版本，不存在可被 resume 误当成真相的半个 turn。

``--replay`` 给了就落盘；不给时走 NullJournal（空对象），调用方零分叉。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Protocol

_SENTENCES = "sentences.jsonl"
_SNAPSHOT = "_snapshot.json"
_META = "meta.json"


class JournalCorruptionError(RuntimeError):
    """日志违反新 Sentence Runtime 格式。

    主文件是原子替换产物；因此这不是可以静默丢弃的“torn 尾行”，
    而是外部篡改或存储损坏，必须 fail-fast。
    """


class Journal(Protocol):
    """一局产物落盘接口。DirJournal 真写，NullJournal 全空操作。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...
    def append_batch(self, sents: list[Any]) -> int: ...
    def last_seq(self) -> int: ...
    def save_snapshot(self, seq: int, state_bytes: bytes) -> None: ...
    def latest_snapshot(self) -> tuple[int, bytes] | None: ...
    def load_sentences(self) -> list[dict[str, Any]]: ...
    def load_batches(self) -> list[list[dict[str, Any]]]: ...
    def close(self) -> None: ...


def _atomic_replace(path: Path, data: bytes) -> None:
    """在 ``path`` 同目录完整写新版本后原子替换，并持久化目录项。

    崩溃留下的 ``.*.tmp`` 从不是真相源；下次提交会直接截断覆盖它。
    运行时契约是单 writer，不在这里再引入第二套锁/事务真相。
    """
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as fp:
        fp.write(data)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _decode_rows(data: bytes) -> list[dict[str, Any]]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise JournalCorruptionError("sentences.jsonl 缺少结尾换行：主文件不是完整提交")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalCorruptionError("sentences.jsonl 不是合法 UTF-8") from exc

    rows: list[dict[str, Any]] = []
    previous_commit = -1
    for index, line in enumerate(text.splitlines()):
        if not line:
            raise JournalCorruptionError(f"sentences.jsonl 第 {index + 1} 行为空")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalCorruptionError(f"sentences.jsonl 第 {index + 1} 行 JSON 损坏") from exc
        if not isinstance(row, dict):
            raise JournalCorruptionError(f"sentences.jsonl 第 {index + 1} 行不是句子对象")
        seq = row.get("seq")
        if type(seq) is not int or seq != index:
            raise JournalCorruptionError(
                f"sentences.jsonl seq 不连续：第 {index + 1} 行期望 {index}，实际 {seq!r}"
            )
        commit_id = row.get("commit_id")
        if type(commit_id) is not int:
            raise JournalCorruptionError(f"sentences.jsonl 第 {index + 1} 行缺少整数 commit_id")
        if index == 0 and commit_id != 0:
            raise JournalCorruptionError(f"sentences.jsonl 首个 commit_id 必须为 0，实际 {commit_id}")
        if index and commit_id not in (previous_commit, previous_commit + 1):
            raise JournalCorruptionError(
                f"sentences.jsonl commit_id 不连续：{previous_commit} 之后出现 {commit_id}"
            )
        previous_commit = commit_id
        rows.append(row)
    return rows


def _group_rows(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    for row in rows:
        if not batches or batches[-1][0]["commit_id"] != row["commit_id"]:
            batches.append([])
        batches[-1].append(row)
    return batches


class DirJournal:
    """一局 = 一个目录：sentences.jsonl + 可选快照。

    resume=True 时只接受新格式的完整日志。新局（resume=False）目录不存在
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

        self._committed = self._sent_path.read_bytes()
        rows = _decode_rows(self._committed)
        self._next_seq = len(rows)
        self._next_commit = rows[-1]["commit_id"] + 1 if rows else 0

    def write_meta(self, meta: dict[str, Any]) -> None:
        data = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_replace(self.run_dir / _META, data)

    def write_avatar(self, seat_no: int, src: Path) -> None:
        avatars = self.run_dir / "avatars"
        avatars.mkdir(exist_ok=True)
        shutil.copyfile(src, avatars / f"{seat_no}{src.suffix}")

    def append_batch(self, sents: list[Any]) -> int:
        """原子提交一个 turn，并在提交前给每条 Sentence 赋 seq/commit_id。"""
        if not sents:
            raise ValueError("append_batch requires at least one sentence")

        commit_id = self._next_commit
        lines: list[str] = []
        for offset, sent in enumerate(sents):
            sent.seq = self._next_seq + offset
            sent.commit_id = commit_id
            lines.append(json.dumps(sent.model_dump(), ensure_ascii=False))
        new_bytes = ("\n".join(lines) + "\n").encode("utf-8")
        committed = self._committed + new_bytes
        _atomic_replace(self._sent_path, committed)

        self._committed = committed
        self._next_seq += len(sents)
        self._next_commit += 1
        return self._next_seq - 1

    def last_seq(self) -> int:
        return self._next_seq - 1

    def save_snapshot(self, seq: int, state_bytes: bytes) -> None:
        """原子保存最新 state 快照；它只能指向当前已提交日志尾。

        语义是“seq 及之前的全部 commit 已 apply 进 state”。它是与日志哈希绑定的
        派生 checkpoint；当前 resume 为验证逻辑完整性会全量重放，不依赖这份文件。
        """
        if seq != self.last_seq():
            raise ValueError(f"snapshot seq must equal committed tail {self.last_seq()}, got {seq}")
        payload = {
            "seq": seq,
            "state": base64.b64encode(state_bytes).decode("ascii"),
            "state_sha256": hashlib.sha256(state_bytes).hexdigest(),
            "log_sha256": hashlib.sha256(self._committed).hexdigest(),
        }
        _atomic_replace(
            self.run_dir / _SNAPSHOT,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def latest_snapshot(self) -> tuple[int, bytes] | None:
        path = self.run_dir / _SNAPSHOT
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            seq = data["seq"]
            state = base64.b64decode(data["state"], validate=True)
            if not isinstance(seq, int):
                return None
            if not isinstance(data["state_sha256"], str) or not isinstance(data["log_sha256"], str):
                return None
            if hashlib.sha256(state).hexdigest() != data["state_sha256"]:
                return None
            lines = self._committed.splitlines(keepends=True)
            if seq < -1 or seq >= len(lines):
                return None
            log_prefix = b"" if seq == -1 else b"".join(lines[: seq + 1])
            if hashlib.sha256(log_prefix).hexdigest() != data["log_sha256"]:
                return None
            return seq, state
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, binascii.Error):
            return None

    def load_sentences(self) -> list[dict[str, Any]]:
        """读出全部已提交句子；临时文件永远不参与读取。"""
        return _decode_rows(self._committed)

    def load_batches(self) -> list[list[dict[str, Any]]]:
        """按日志内 commit_id 返回原子 turn，不从 turn_id 猜测边界。"""
        return _group_rows(self.load_sentences())

    def close(self) -> None:
        """无长寿命文件句柄；每次提交都是独立的原子替换。"""


class NullJournal:
    """不落盘，但仍分配 seq/commit_id，保证 renderer 与有日志时语义一致。"""

    def __init__(self) -> None:
        self._next_seq = 0
        self._next_commit = 0

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...

    def append_batch(self, sents: list[Any]) -> int:
        if not sents:
            raise ValueError("append_batch requires at least one sentence")
        for sent in sents:
            sent.seq = self._next_seq
            sent.commit_id = self._next_commit
            self._next_seq += 1
        self._next_commit += 1
        return self._next_seq - 1

    def last_seq(self) -> int:
        return self._next_seq - 1

    def save_snapshot(self, seq: int, state_bytes: bytes) -> None: ...

    def latest_snapshot(self) -> tuple[int, bytes] | None:
        return None

    def load_sentences(self) -> list[dict[str, Any]]:
        return []

    def load_batches(self) -> list[list[dict[str, Any]]]:
        return []

    def close(self) -> None: ...


def open_journal(replay: Path | None, *, resume: bool = False) -> Journal:
    """--replay 给了就写该目录；不给返回 NullJournal。"""
    return DirJournal(replay, resume=resume) if replay is not None else NullJournal()
