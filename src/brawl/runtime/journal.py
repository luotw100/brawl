"""句子日志 = 一局的唯一真相与 checkpoint。

磁盘上的 ``sentences.jsonl`` 一行一句；完整 expectation 共享 ``commit_id``，
并通过同目录原子替换一次提交。所有读取都经过同一个严格边界，resume、console、
TTS 不再各自猜测 JSONL 格式。
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from brawl.runtime.files import UnsafePathError, atomic_create, atomic_write, read_regular_bytes
from brawl.runtime.types import Sentence

_SENTENCES = "sentences.jsonl"
_META = "meta.json"
_LOCK = ".journal.lock"


class JournalCorruptionError(RuntimeError):
    """日志不是完整、严格的 Sentence Runtime 格式。"""


class JournalInUseError(RuntimeError):
    """同一 replay 已有另一个生成或续跑进程持有写锁。"""


def _acquire_writer_lock(run_dir: Path) -> int:
    """取得 replay 生命周期独占锁；返回必须由调用方关闭的 fd。"""
    dir_fd = -1
    lock_fd = -1
    try:
        try:
            dir_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafePathError(f"拒绝符号链接或非目录 replay：{run_dir}") from exc
            raise
        try:
            lock_fd = os.open(
                _LOCK,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafePathError(f"拒绝符号链接 journal 锁：{run_dir / _LOCK}") from exc
            raise
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise UnsafePathError(f"journal 锁不是普通文件：{run_dir / _LOCK}")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JournalInUseError(f"同一 replay 已有生成或续跑任务在运行：{run_dir}") from exc
        acquired = lock_fd
        lock_fd = -1
        return acquired
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if dir_fd >= 0:
            os.close(dir_fd)


class Journal(Protocol):
    """一局产物落盘接口。DirJournal 真写，NullJournal 全空操作。"""

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...
    def append_batch(self, sents: list[Sentence]) -> int: ...
    def load_sentences(self) -> list[dict[str, Any]]: ...
    def load_batches(self) -> list[list[dict[str, Any]]]: ...
    def close(self) -> None: ...


def _decode_sentences(data: bytes) -> list[Sentence]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise JournalCorruptionError("sentences.jsonl 缺少结尾换行：主文件不是完整提交")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalCorruptionError("sentences.jsonl 不是合法 UTF-8") from exc

    sentences: list[Sentence] = []
    previous_commit = -1
    # 只能按 JSONL 真正的 LF 分隔。str.splitlines() 还会把合法字符串里的
    # U+0085/U+2028/U+2029 当成行边界，导致刚写出的日志无法再次读取。
    for index, line in enumerate(text[:-1].split("\n")):
        if not line:
            raise JournalCorruptionError(f"sentences.jsonl 第 {index + 1} 行为空")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalCorruptionError(f"sentences.jsonl 第 {index + 1} 行 JSON 损坏") from exc
        try:
            sentence = Sentence.model_validate(row, strict=True)
        except ValidationError as exc:
            raise JournalCorruptionError(
                f"sentences.jsonl 第 {index + 1} 行不是合法 Sentence"
            ) from exc

        if sentence.seq != index:
            raise JournalCorruptionError(
                f"sentences.jsonl seq 不连续：第 {index + 1} 行期望 {index}，"
                f"实际 {sentence.seq!r}"
            )
        commit_id = sentence.commit_id
        if commit_id < 0:
            raise JournalCorruptionError(
                f"sentences.jsonl 第 {index + 1} 行 commit_id 不能为负数"
            )
        if index == 0 and commit_id != 0:
            raise JournalCorruptionError(
                f"sentences.jsonl 首个 commit_id 必须为 0，实际 {commit_id}"
            )
        if index and commit_id not in (previous_commit, previous_commit + 1):
            raise JournalCorruptionError(
                f"sentences.jsonl commit_id 不连续：{previous_commit} 之后出现 {commit_id}"
            )
        previous_commit = commit_id
        sentences.append(sentence)
    return sentences


def _group_rows(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    for row in rows:
        if not batches or batches[-1][0]["commit_id"] != row["commit_id"]:
            batches.append([])
        batches[-1].append(row)
    return batches


def load_replay_sentences(replay_dir: Path) -> list[Sentence]:
    """统一的 replay 输入边界，供 resume、console、TTS 等消费者复用。"""
    return _decode_sentences(read_regular_bytes(replay_dir / _SENTENCES))


def _prepare_batch(
    sents: list[Sentence],
    *,
    next_seq: int,
    commit_id: int,
) -> list[Sentence]:
    if not sents:
        raise ValueError("append_batch 至少需要一条句子")
    prepared: list[Sentence] = []
    for offset, sent in enumerate(sents):
        try:
            row = sent.model_dump()
            row.update(seq=next_seq + offset, commit_id=commit_id)
            prepared.append(Sentence.model_validate(row, strict=True))
        except (AttributeError, ValidationError) as exc:
            raise ValueError(f"append_batch 第 {offset + 1} 项不是合法 Sentence") from exc
    return prepared


def _publish_runtime_ids(originals: list[Sentence], prepared: list[Sentence]) -> None:
    for original, written in zip(originals, prepared, strict=True):
        original.seq = written.seq
        original.commit_id = written.commit_id


class DirJournal:
    """一局目录中的严格 Sentence 日志；实例生命周期独占 replay 写权。"""

    def __init__(self, run_dir: Path, *, resume: bool = False) -> None:
        self.run_dir = run_dir
        self._lock_fd = -1
        self._sent_path = run_dir / _SENTENCES
        if not resume:
            run_dir.mkdir(parents=True, exist_ok=False)
        try:
            # 必须先锁后读；否则两个 resume 实例会从同一旧快照出发并互相覆盖。
            self._lock_fd = _acquire_writer_lock(run_dir)
            if resume:
                self._committed = read_regular_bytes(self._sent_path)
            else:
                atomic_write(self._sent_path, b"")
                self._committed = b""

            sentences = _decode_sentences(self._committed)
            self._next_seq = len(sentences)
            self._next_commit = sentences[-1].commit_id + 1 if sentences else 0
        except BaseException:
            self.close()
            raise

    def write_meta(self, meta: dict[str, Any]) -> None:
        data = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_create(self.run_dir / _META, data)

    def write_avatar(self, seat_no: int, src: Path) -> None:
        avatars = self.run_dir / "avatars"
        avatars.mkdir(exist_ok=True)
        atomic_write(avatars / f"{seat_no}{src.suffix}", src.read_bytes())

    def append_batch(self, sents: list[Sentence]) -> int:
        """原子提交一个 turn；成功后才把 seq/commit_id 发布给调用者对象。"""
        prepared = _prepare_batch(
            sents,
            next_seq=self._next_seq,
            commit_id=self._next_commit,
        )
        new_bytes = (
            "\n".join(
                json.dumps(sentence.model_dump(), ensure_ascii=False)
                for sentence in prepared
            )
            + "\n"
        ).encode("utf-8")
        committed = self._committed + new_bytes
        atomic_write(self._sent_path, committed)

        _publish_runtime_ids(sents, prepared)
        self._committed = committed
        self._next_seq += len(prepared)
        self._next_commit += 1
        return self._next_seq - 1

    def load_sentences(self) -> list[dict[str, Any]]:
        """读出全部已提交句子；临时文件永远不参与读取。"""
        return [sentence.model_dump() for sentence in _decode_sentences(self._committed)]

    def load_batches(self) -> list[list[dict[str, Any]]]:
        """按日志内 commit_id 返回原子 turn，不从 turn_id 猜测边界。"""
        return _group_rows(self.load_sentences())

    def close(self) -> None:
        """释放 replay 写权；幂等。"""
        if self._lock_fd < 0:
            return
        lock_fd = self._lock_fd
        self._lock_fd = -1
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def __del__(self) -> None:
        # 正常所有权必须显式 close；这里只防异常路径/交互式调用泄漏进程级锁。
        with suppress(Exception):
            self.close()


class NullJournal:
    """不落盘，但仍分配 seq/commit_id，保证 live 渲染语义一致。"""

    def __init__(self) -> None:
        self._next_seq = 0
        self._next_commit = 0

    def write_meta(self, meta: dict[str, Any]) -> None: ...
    def write_avatar(self, seat_no: int, src: Path) -> None: ...

    def append_batch(self, sents: list[Sentence]) -> int:
        prepared = _prepare_batch(
            sents,
            next_seq=self._next_seq,
            commit_id=self._next_commit,
        )
        _publish_runtime_ids(sents, prepared)
        self._next_seq += len(prepared)
        self._next_commit += 1
        return self._next_seq - 1

    def load_sentences(self) -> list[dict[str, Any]]:
        return []

    def load_batches(self) -> list[list[dict[str, Any]]]:
        return []

    def close(self) -> None: ...


def open_journal(replay: Path | None, *, resume: bool = False) -> Journal:
    """--replay 给了就写该目录；不给返回 NullJournal。"""
    return DirJournal(replay, resume=resume) if replay is not None else NullJournal()
