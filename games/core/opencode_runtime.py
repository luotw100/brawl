"""Avalon/Werewolf owned OpenCode server lifecycle and filesystem isolation."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import httpx

EXPECTED_OPENCODE_VERSION = "1.17.18"
RUNTIME_DIR = "_opencode"

_SERVER_USERNAME = "opencode"
_START_TIMEOUT = 30.0
_STOP_TIMEOUT = 5.0
_LISTEN_RE = re.compile(r"opencode server listening on (http://127\.0\.0\.1:\d+)")


class OpenCodeRuntimeError(Exception):
    """Managed OpenCode cannot be prepared, started, or stopped safely."""


@dataclass(frozen=True)
class OpenCodeInstallation:
    executable: Path
    sandbox: Path
    auth_file: Path
    version: str


@dataclass(frozen=True)
class OpenCodeConnection:
    base_url: str
    username: str
    password: str
    workspace_root: str


@dataclass(frozen=True)
class _Layout:
    root: Path
    auth_mount: Path
    log: Path


def prepare_opencode_installation() -> OpenCodeInstallation:
    """Fail fast before creating a fresh replay directory."""
    executable_raw = shutil.which("opencode")
    if executable_raw is None:
        raise OpenCodeRuntimeError("找不到 opencode 可执行文件，请先安装 OpenCode 1.17.18")
    sandbox_raw = shutil.which("bwrap")
    if sandbox_raw is None:
        raise OpenCodeRuntimeError("找不到 bwrap；OpenCode 玩家必须在文件系统沙箱内运行")

    executable = Path(executable_raw).resolve()
    sandbox = Path(sandbox_raw).resolve()
    version_result = _run_preflight([str(executable), "--version"], "读取 OpenCode 版本失败")
    version = version_result.stdout.strip()
    if version != EXPECTED_OPENCODE_VERSION:
        raise OpenCodeRuntimeError(
            f"OpenCode 版本必须是 {EXPECTED_OPENCODE_VERSION}，当前是 {version or '未知'}"
        )

    auth_file = _host_auth_file()
    if not auth_file.is_file():
        raise OpenCodeRuntimeError(f"缺少 OpenCode 认证文件：{auth_file}；请先执行 opencode auth login")

    _run_preflight(
        [str(sandbox), "--die-with-parent", "--ro-bind", "/", "/", "/usr/bin/true"],
        "bwrap 文件系统沙箱不可用",
    )
    return OpenCodeInstallation(
        executable=executable,
        sandbox=sandbox,
        auth_file=auth_file,
        version=version,
    )


def _run_preflight(command: list[str], message: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OpenCodeRuntimeError(f"{message}：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise OpenCodeRuntimeError(f"{message}：{detail or f'退出码 {result.returncode}'}")
    return result


def _host_auth_file() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return root / "opencode" / "auth.json"


def _prepare_layout(run_dir: Path, seats: int) -> _Layout:
    try:
        replay_root = run_dir.resolve(strict=True)
    except OSError as exc:
        raise OpenCodeRuntimeError(f"replay 目录不可用：{run_dir}：{exc}") from exc
    if not replay_root.is_dir():
        raise OpenCodeRuntimeError(f"replay 路径不是目录：{run_dir}")

    root = replay_root / RUNTIME_DIR
    directories = [
        root,
        root / "storage",
        root / "data",
        root / "data" / "opencode",
        root / "config",
        root / "state",
        root / "cache",
        root / "home",
        root / "workspaces",
        root / "logs",
    ]
    directories.extend(root / "workspaces" / f"seat-{seat_no:02d}" for seat_no in range(1, seats + 1))
    for directory in directories:
        _prepare_private_directory(directory)

    auth_mount = root / "data" / "opencode" / "auth.json"
    if auth_mount.is_symlink():
        raise OpenCodeRuntimeError(f"OpenCode 隔离目录不允许软链接：{auth_mount}")
    try:
        fd = os.open(
            auth_mount,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
        )
    except FileExistsError:
        pass
    except OSError as exc:
        raise OpenCodeRuntimeError(f"无法创建隔离认证挂载点：{auth_mount}：{exc}") from exc
    else:
        os.close(fd)
    auth_stat = auth_mount.lstat()
    if not stat.S_ISREG(auth_stat.st_mode) or auth_stat.st_nlink != 1 or auth_stat.st_size:
        raise OpenCodeRuntimeError(f"隔离认证挂载点不应包含数据：{auth_mount}")
    auth_mount.chmod(0o400)
    return _Layout(root=root, auth_mount=auth_mount, log=_next_log_path(root / "logs"))


def _prepare_private_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise OpenCodeRuntimeError(f"OpenCode 隔离目录不允许软链接：{directory}")
    try:
        directory.mkdir(exist_ok=True, mode=0o700)
    except OSError as exc:
        raise OpenCodeRuntimeError(f"无法创建 OpenCode 隔离目录：{directory}：{exc}") from exc
    if directory.is_symlink() or not directory.is_dir():
        raise OpenCodeRuntimeError(f"OpenCode 隔离路径不是普通目录：{directory}")
    directory.chmod(0o700)


def _next_log_path(log_dir: Path) -> Path:
    attempt = 1
    while True:
        path = log_dir / f"serve-{attempt:03d}.log"
        if not path.exists() and not path.is_symlink():
            return path
        attempt += 1


def _sandbox_command(installation: OpenCodeInstallation, layout: _Layout) -> list[str]:
    command = [
        str(installation.sandbox),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs",
        "--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf",
        "--ro-bind", "/etc/hosts", "/etc/hosts",
        "--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", str(layout.root), "/oc",
        "--ro-bind", str(installation.auth_file), "/oc/data/opencode/auth.json",
        "--ro-bind", str(installation.executable), "/opencode",
        "--chdir", "/oc/workspaces",
        "/opencode",
        "serve",
        "--hostname", "127.0.0.1",
        "--port", "0",
        "--pure",
        "--print-logs",
        "--log-level", "INFO",
    ]
    return command


def _sandbox_env(password: str) -> dict[str, str]:
    return {
        "HOME": "/oc/home",
        "XDG_DATA_HOME": "/oc/data",
        "XDG_CONFIG_HOME": "/oc/config",
        "XDG_STATE_HOME": "/oc/state",
        "XDG_CACHE_HOME": "/oc/cache",
        "OPENCODE_DB": "/oc/storage/opencode.db",
        "OPENCODE_SERVER_USERNAME": _SERVER_USERNAME,
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }


@asynccontextmanager
async def open_opencode_runtime(
    installation: OpenCodeInstallation,
    run_dir: Path,
    seats: int,
) -> AsyncIterator[OpenCodeConnection]:
    """Start one isolated server for a game and always reap its process tree."""
    layout = _prepare_layout(run_dir, seats)
    password = secrets.token_urlsafe(32)
    command = _sandbox_command(installation, layout)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_sandbox_env(password),
            start_new_session=True,
        )
    except OSError as exc:
        raise OpenCodeRuntimeError(f"启动 OpenCode 失败：{exc}；日志目录：{layout.log.parent}") from exc

    assert process.stdout is not None
    ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    reader = asyncio.create_task(_capture_output(process, layout.log, ready))
    deadline = time.monotonic() + _START_TIMEOUT
    try:
        try:
            base_url = await asyncio.wait_for(asyncio.shield(ready), timeout=_START_TIMEOUT)
        except TimeoutError as exc:
            raise OpenCodeRuntimeError(f"OpenCode 启动超时；日志：{layout.log}") from exc
        await _wait_for_health(base_url, password, installation.version, deadline, layout.log)
        yield OpenCodeConnection(
            base_url=base_url,
            username=_SERVER_USERNAME,
            password=password,
            workspace_root="/oc/workspaces",
        )
    finally:
        if not ready.done():
            ready.cancel()
        await _finish_runtime(process, reader)


async def _capture_output(
    process: asyncio.subprocess.Process,
    log_path: Path,
    ready: asyncio.Future[str],
) -> None:
    assert process.stdout is not None
    try:
        with log_path.open("x", encoding="utf-8") as log:
            os.fchmod(log.fileno(), 0o600)
            while line := await process.stdout.readline():
                text = line.decode("utf-8", errors="replace")
                log.write(text)
                log.flush()
                match = _LISTEN_RE.search(text)
                if match is not None and not ready.done():
                    ready.set_result(match.group(1))
    except Exception as exc:
        if not ready.done():
            ready.set_exception(OpenCodeRuntimeError(f"读取 OpenCode 启动日志失败：{exc}"))
        raise
    finally:
        if not ready.done():
            returncode = await process.wait()
            ready.set_exception(OpenCodeRuntimeError(f"OpenCode 启动前退出（退出码 {returncode}）；日志：{log_path}"))


async def _wait_for_health(
    base_url: str,
    password: str,
    expected_version: str,
    deadline: float,
    log_path: Path,
) -> None:
    async with httpx.AsyncClient(
        base_url=base_url,
        auth=(_SERVER_USERNAME, password),
        timeout=1.0,
        trust_env=False,
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get("/global/health")
                if response.status_code == 200:
                    payload = response.json()
                    version = payload.get("version")
                    if version != expected_version:
                        raise OpenCodeRuntimeError(
                            f"OpenCode health 版本异常：期望 {expected_version}，收到 {version}"
                        )
                    if payload.get("healthy") is True:
                        return
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.1)
    raise OpenCodeRuntimeError(f"OpenCode health check 超时；日志：{log_path}")


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def _finish_runtime(process: asyncio.subprocess.Process, reader: asyncio.Task[None]) -> None:
    """Reap the server even if the owner task is cancelled again during cleanup."""

    async def cleanup() -> None:
        await _stop_process(process)
        await reader

    cleanup_task = asyncio.create_task(cleanup())
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    await cleanup_task
    if cancelled:
        raise asyncio.CancelledError
