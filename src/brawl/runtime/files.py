"""Sentence Runtime 的安全文件边界。

所有派生产物都通过同目录随机临时文件原子替换：临时文件用 ``O_EXCL`` 创建，
写完 fsync 文件，替换后再 fsync 目录。目录和已有目标都拒绝符号链接，避免 replay
目录中的链接把写入重定向到目录外。
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path


class UnsafePathError(RuntimeError):
    """文件边界遇到符号链接或非普通文件。"""


def _open_parent(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(path.parent, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError(f"拒绝符号链接或非目录父路径：{path.parent}") from exc
        raise


def _assert_regular_target(dir_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"拒绝写入符号链接：{name}")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePathError(f"拒绝覆盖非普通文件：{name}")


def _assert_absent_target(dir_fd: int, name: str) -> None:
    """校验目标尚未存在，且把已有符号链接当成安全边界错误。"""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"拒绝写入符号链接：{name}")
    raise FileExistsError(errno.EEXIST, "目标已存在", name)


def read_regular_bytes(path: Path) -> bytes:
    """无跟随链接地读取一个普通文件。"""
    dir_fd = _open_parent(path)
    fd = -1
    try:
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafePathError(f"拒绝读取符号链接：{path}") from exc
            raise
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePathError(f"拒绝读取非普通文件：{path}")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)


def atomic_write(path: Path, data: bytes) -> None:
    """安全、持久且原子地把 ``data`` 写到 ``path``。

    目标可不存在或是普通文件；符号链接、目录、设备等一律在边界拒绝。
    """
    dir_fd = _open_parent(path)
    tmp_name = ""
    fd = -1
    try:
        _assert_regular_target(dir_fd, path.name)
        while True:
            tmp_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
            try:
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=dir_fd,
                )
                break
            except FileExistsError:
                continue

        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1

        # 临近替换再验一次；即使攻击者在此后抢占，replace 也只会替换链接本身，
        # 不会跟随它写到链接目标。
        _assert_regular_target(dir_fd, path.name)
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp_name = ""
        os.fsync(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name:
            with suppress(FileNotFoundError):
                os.unlink(tmp_name, dir_fd=dir_fd)
        os.close(dir_fd)


def atomic_create(path: Path, data: bytes) -> None:
    """仅在目标不存在时原子发布完整文件。

    先在同目录写完并 fsync 随机临时文件，再用硬链接的
    create-if-absent 语义发布。已有普通文件时抛 FileExistsError，
    已有符号链接或非安全父路径时抛 UnsafePathError。
    """
    dir_fd = _open_parent(path)
    tmp_name = ""
    fd = -1
    try:
        _assert_absent_target(dir_fd, path.name)
        while True:
            tmp_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
            try:
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=dir_fd,
                )
                break
            except FileExistsError:
                continue

        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1

        try:
            os.link(
                tmp_name,
                path.name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            # 初检之后若被抢占，再区分符号链接与普通的已存在文件。
            _assert_absent_target(dir_fd, path.name)
            raise

        os.unlink(tmp_name, dir_fd=dir_fd)
        tmp_name = ""
        os.fsync(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name:
            with suppress(FileNotFoundError):
                os.unlink(tmp_name, dir_fd=dir_fd)
        os.close(dir_fd)
