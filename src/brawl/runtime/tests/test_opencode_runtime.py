"""OpenCode 沙箱布局：模型目录快照与可信 agent/tool 配置播种。

沙箱禁了模型目录拉取，快照来自宿主机缓存；宿主没有快照时必须静默维持旧行为，
沙箱侧出现软链接必须 fail-stop（与 auth 挂载点同款防线）。可信配置只接受相对
普通文件，内容一经落盘不得被下一次启动静默覆盖。
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from brawl.runtime.opencode_runtime import (
    OpenCodeInstallation,
    OpenCodeRuntimeError,
    _prepare_layout,
    _sandbox_command,
    _sandbox_env,
    _seed_models_cache,
)

_CATALOG = '{"opencode-go": {"models": {"hy3": {}}}}'


def _host_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = tmp_path / "host-cache" / "opencode" / "models.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(_CATALOG, encoding="utf-8")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "host-cache"))


def test_seed_copies_host_snapshot_into_sandbox_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _host_catalog(tmp_path, monkeypatch)
    root = tmp_path / "runtime"
    (root / "cache").mkdir(parents=True)

    _seed_models_cache(root)

    dst = root / "cache" / "opencode" / "models.json"
    assert dst.read_text(encoding="utf-8") == _CATALOG
    assert not dst.is_symlink()


def test_seed_refreshes_stale_snapshot_on_every_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _host_catalog(tmp_path, monkeypatch)
    root = tmp_path / "runtime"
    dst = root / "cache" / "opencode" / "models.json"
    dst.parent.mkdir(parents=True)
    dst.write_text('{"旧": true}', encoding="utf-8")

    _seed_models_cache(root)

    assert dst.read_text(encoding="utf-8") == _CATALOG


def test_seed_skips_silently_when_host_has_no_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "empty-cache"))
    root = tmp_path / "runtime"
    root.mkdir()

    _seed_models_cache(root)

    assert not (root / "cache" / "opencode" / "models.json").exists()


def test_seed_skips_host_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _host_catalog(tmp_path, monkeypatch)
    catalog = tmp_path / "host-cache" / "opencode" / "models.json"
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    catalog.unlink()
    catalog.symlink_to(target)
    root = tmp_path / "runtime"
    root.mkdir()

    _seed_models_cache(root)

    assert not (root / "cache" / "opencode" / "models.json").exists()


def test_seed_refuses_sandbox_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _host_catalog(tmp_path, monkeypatch)
    root = tmp_path / "runtime"
    dst = root / "cache" / "opencode" / "models.json"
    dst.parent.mkdir(parents=True)
    target = tmp_path / "evil.json"
    target.write_text("{}", encoding="utf-8")
    dst.symlink_to(target)

    with pytest.raises(OpenCodeRuntimeError, match="软链接"):
        _seed_models_cache(root)


def _empty_host_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "empty-host-cache"))


def _replay_dir(tmp_path: Path) -> Path:
    replay = tmp_path / "replay"
    replay.mkdir()
    return replay


def test_trusted_config_writes_private_files_and_compaction_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_host_cache(tmp_path, monkeypatch)
    replay = _replay_dir(tmp_path)

    layout = _prepare_layout(
        replay,
        seats=2,
        trusted_config={
            "agents/avalon-servant.md": "---\nmode: primary\n---\n规则",
            "tools/submit.ts": "export const submit = {}",
        },
    )

    assert layout.trusted_config == replay / "_opencode" / "trusted-config"
    assert stat.S_IMODE(layout.trusted_config.stat().st_mode) == 0o700
    agent = layout.trusted_config / "agents" / "avalon-servant.md"
    tool = layout.trusted_config / "tools" / "submit.ts"
    config = layout.trusted_config / "opencode.json"
    assert agent.read_text(encoding="utf-8") == "---\nmode: primary\n---\n规则"
    assert tool.read_text(encoding="utf-8") == "export const submit = {}"
    assert stat.S_IMODE(agent.stat().st_mode) == 0o600
    assert stat.S_IMODE(tool.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert json.loads(config.read_text(encoding="utf-8"))["compaction"] == {
        "auto": False,
        "prune": False,
    }


def test_trusted_config_preserves_other_opencode_json_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_host_cache(tmp_path, monkeypatch)
    replay = _replay_dir(tmp_path)

    layout = _prepare_layout(
        replay,
        seats=1,
        trusted_config={
            "opencode.json": json.dumps(
                {
                    "share": "disabled",
                    "compaction": {"reserved": 123, "auto": True},
                }
            )
        },
    )

    assert layout.trusted_config is not None
    config = json.loads(
        (layout.trusted_config / "opencode.json").read_text(encoding="utf-8")
    )
    assert config == {
        "share": "disabled",
        "compaction": {"reserved": 123, "auto": False, "prune": False},
    }


def test_trusted_config_same_content_is_reusable_but_different_content_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_host_cache(tmp_path, monkeypatch)
    replay = _replay_dir(tmp_path)
    bundle = {"agents/avalon-servant.md": "same"}

    first = _prepare_layout(replay, seats=1, trusted_config=bundle)
    second = _prepare_layout(replay, seats=1, trusted_config=bundle)

    assert first.trusted_config == second.trusted_config
    with pytest.raises(OpenCodeRuntimeError, match="内容不同"):
        _prepare_layout(
            replay,
            seats=1,
            trusted_config={"agents/avalon-servant.md": "changed"},
        )


@pytest.mark.parametrize(
    "unsafe",
    [
        "/absolute/agent.md",
        "../agent.md",
        "agents/../../agent.md",
    ],
)
def test_trusted_config_rejects_absolute_and_parent_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    _empty_host_cache(tmp_path, monkeypatch)
    replay = _replay_dir(tmp_path)

    with pytest.raises(OpenCodeRuntimeError, match="相对路径"):
        _prepare_layout(replay, seats=1, trusted_config={unsafe: "x"})


def test_trusted_config_rejects_jsonc_that_could_override_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_host_cache(tmp_path, monkeypatch)
    replay = _replay_dir(tmp_path)

    with pytest.raises(OpenCodeRuntimeError, match="opencode.jsonc"):
        _prepare_layout(
            replay,
            seats=1,
            trusted_config={"./opencode.jsonc": '{"compaction":{"auto":true}}'},
        )


@pytest.mark.parametrize("link_kind", ["directory", "file"])
def test_trusted_config_rejects_symlink_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_kind: str,
) -> None:
    _empty_host_cache(tmp_path, monkeypatch)
    replay = _replay_dir(tmp_path)
    config = replay / "_opencode" / "trusted-config"
    config.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if link_kind == "directory":
        (config / "agents").symlink_to(outside, target_is_directory=True)
    else:
        target = outside / "agent.md"
        target.write_text("x", encoding="utf-8")
        (config / "agent.md").symlink_to(target)

    with pytest.raises(OpenCodeRuntimeError, match="软链接"):
        _prepare_layout(
            replay,
            seats=1,
            trusted_config={"agents/avalon-servant.md" if link_kind == "directory" else "agent.md": "x"},
        )


def test_sandbox_env_only_exposes_trusted_config_when_bundle_exists() -> None:
    plain = _sandbox_env("secret")
    trusted = _sandbox_env("secret", trusted_config=True)

    assert "OPENCODE_CONFIG_DIR" not in plain
    assert trusted["OPENCODE_CONFIG_DIR"] == "/oc/trusted-config"
    assert trusted["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    # Compaction is an inspectable opencode.json policy, not an invented flag.
    assert "OPENCODE_DISABLE_AUTOCOMPACT" not in trusted
    assert "OPENCODE_DISABLE_PRUNE" not in trusted


def test_sandbox_command_keeps_pure_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _empty_host_cache(tmp_path, monkeypatch)
    replay = _replay_dir(tmp_path)
    layout = _prepare_layout(replay, seats=1, trusted_config={})
    installation = OpenCodeInstallation(
        executable=Path("/fake/opencode"),
        sandbox=Path("/fake/bwrap"),
        auth_file=Path("/fake/auth.json"),
        version="1.17.20",
    )

    command = _sandbox_command(installation, layout)

    assert "--pure" in command
