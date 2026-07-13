from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from core import opencode_runtime as runtime


def _installation(tmp_path: Path) -> runtime.OpenCodeInstallation:
    executable = tmp_path / "opencode"
    sandbox = tmp_path / "bwrap"
    auth = tmp_path / "host-data" / "opencode" / "auth.json"
    executable.touch()
    sandbox.touch()
    auth.parent.mkdir(parents=True)
    auth.write_text('{"provider":"secret"}', encoding="utf-8")
    return runtime.OpenCodeInstallation(
        executable=executable,
        sandbox=sandbox,
        auth_file=auth,
        version=runtime.EXPECTED_OPENCODE_VERSION,
    )


def test_prepare_installation_pins_version_and_checks_sandbox(tmp_path, monkeypatch) -> None:
    installation = _installation(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(installation.auth_file.parents[1]))
    monkeypatch.setattr(
        runtime.shutil,
        "which",
        lambda name: str(installation.executable if name == "opencode" else installation.sandbox),
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stdout = runtime.EXPECTED_OPENCODE_VERSION if command[-1] == "--version" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)

    prepared = runtime.prepare_opencode_installation()

    assert prepared == installation
    assert calls[0] == [str(installation.executable), "--version"]
    assert calls[1][0] == str(installation.sandbox)
    assert calls[1][-1] == "/usr/bin/true"


def test_prepare_installation_rejects_version_drift(tmp_path, monkeypatch) -> None:
    installation = _installation(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(installation.auth_file.parents[1]))
    monkeypatch.setattr(
        runtime.shutil,
        "which",
        lambda name: str(installation.executable if name == "opencode" else installation.sandbox),
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="1.18.0", stderr=""),
    )

    with pytest.raises(runtime.OpenCodeRuntimeError, match="必须是 1.17.18"):
        runtime.prepare_opencode_installation()


def test_layout_and_command_keep_all_generated_state_under_runtime_dir(tmp_path) -> None:
    run_dir = tmp_path / "replay"
    run_dir.mkdir()
    installation = _installation(tmp_path)

    first = runtime._prepare_layout(run_dir, seats=2)
    sentinel = first.root / "storage" / "opencode.db"
    sentinel.write_bytes(b"keep")
    first.log.write_text("old log", encoding="utf-8")
    second = runtime._prepare_layout(run_dir, seats=2)
    command = runtime._sandbox_command(installation, second)
    env = runtime._sandbox_env("random-password")

    assert sentinel.read_bytes() == b"keep"
    assert second.log.name == "serve-002.log"
    assert first.auth_mount.read_bytes() == b""
    assert all((first.root / "workspaces" / f"seat-{seat:02d}").is_dir() for seat in (1, 2))
    assert [str(first.root), "/oc"] == command[command.index("--bind") + 1:command.index("--bind") + 3]
    assert str(run_dir) not in [
        target for source, target in zip(command, command[1:], strict=False) if source == "--ro-bind"
    ]
    assert command[-10:] == [
        "/opencode", "serve", "--hostname", "127.0.0.1", "--port", "0",
        "--pure", "--print-logs", "--log-level", "INFO",
    ]
    assert "random-password" not in command
    assert env["OPENCODE_SERVER_PASSWORD"] == "random-password"
    assert env["OPENCODE_DB"] == "/oc/storage/opencode.db"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["HOME"] == "/oc/home"


@pytest.mark.parametrize("linked_path", ["_opencode", "_opencode/data"])
def test_layout_rejects_symlinked_isolation_directories(tmp_path, linked_path) -> None:
    run_dir = tmp_path / "replay"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = run_dir / linked_path
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(runtime.OpenCodeRuntimeError, match="不允许软链接"):
        runtime._prepare_layout(run_dir, seats=2)


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 43210
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"opencode server listening on http://127.0.0.1:45678\n")
        self._done = asyncio.Event()

    def finish(self) -> None:
        self.returncode = 0
        self.stdout.feed_eof()
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode


async def test_runtime_yields_managed_connection_and_reaps_process(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "replay"
    run_dir.mkdir()
    installation = _installation(tmp_path)
    process = _FakeProcess()
    spawned: dict[str, Any] = {}
    signals: list[tuple[int, signal.Signals]] = []

    async def fake_spawn(*command: str, **kwargs: Any) -> _FakeProcess:
        spawned["command"] = list(command)
        spawned["kwargs"] = kwargs
        return process

    async def fake_health(*args: Any, **kwargs: Any) -> None:
        return None

    def fake_killpg(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        process.finish()

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(runtime, "_wait_for_health", fake_health)
    monkeypatch.setattr(runtime.os, "killpg", fake_killpg)
    monkeypatch.setattr(runtime.secrets, "token_urlsafe", lambda _: "runtime-secret")

    async with runtime.open_opencode_runtime(installation, run_dir, seats=2) as connection:
        assert connection.base_url == "http://127.0.0.1:45678"
        assert connection.password == "runtime-secret"
        assert connection.workspace_root == "/oc/workspaces"
        assert process.returncode is None

    assert signals == [(process.pid, signal.SIGTERM)]
    assert process.returncode == 0
    assert spawned["kwargs"]["start_new_session"] is True
    assert spawned["kwargs"]["env"]["OPENCODE_SERVER_PASSWORD"] == "runtime-secret"
    assert "runtime-secret" not in spawned["command"]
    assert (run_dir / "_opencode" / "logs" / "serve-001.log").is_file()


async def test_runtime_reaps_process_after_repeated_cancellation(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "replay"
    run_dir.mkdir()
    installation = _installation(tmp_path)
    process = _FakeProcess()
    entered = asyncio.Event()
    stopping = asyncio.Event()
    release_stop = asyncio.Event()

    async def fake_spawn(*command: str, **kwargs: Any) -> _FakeProcess:
        return process

    async def fake_health(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_stop(target: _FakeProcess) -> None:
        stopping.set()
        await release_stop.wait()
        target.finish()

    async def run() -> None:
        async with runtime.open_opencode_runtime(installation, run_dir, seats=1):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(runtime, "_wait_for_health", fake_health)
    monkeypatch.setattr(runtime, "_stop_process", fake_stop)

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    await stopping.wait()
    task.cancel()
    release_stop.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == 0


def test_async_main_allows_cleanup_after_repeated_sigint(tmp_path) -> None:
    marker = tmp_path / "signals.txt"
    script = """
import asyncio
import sys
from pathlib import Path
from core.runner import run_async_main

marker = Path(sys.argv[1])

async def main():
    marker.write_text("started\\n", encoding="utf-8")
    try:
        await asyncio.Event().wait()
    finally:
        with marker.open("a", encoding="utf-8") as stream:
            stream.write("cleanup-start\\n")
        await asyncio.sleep(0.3)
        with marker.open("a", encoding="utf-8") as stream:
            stream.write("cleanup-done\\n")

try:
    run_async_main(main())
except KeyboardInterrupt:
    with marker.open("a", encoding="utf-8") as stream:
        stream.write("interrupted\\n")
    raise SystemExit(130)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(marker)])
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()

    try:
        os.kill(process.pid, signal.SIGINT)
        deadline = time.monotonic() + 2
        while "cleanup-start" not in marker.read_text(encoding="utf-8") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "cleanup-start" in marker.read_text(encoding="utf-8")
        os.kill(process.pid, signal.SIGINT)

        assert process.wait(timeout=5) == 130
        assert marker.read_text(encoding="utf-8").splitlines() == [
            "started",
            "cleanup-start",
            "cleanup-done",
            "interrupted",
        ]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
