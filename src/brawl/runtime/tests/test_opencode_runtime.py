"""OpenCode 沙箱布局：models.dev 目录快照播种（_seed_models_cache）。

沙箱禁了模型目录拉取，快照来自宿主机缓存；宿主没有快照时必须静默维持旧行为，
沙箱侧出现软链接必须 fail-stop（与 auth 挂载点同款防线）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brawl.runtime.opencode_runtime import OpenCodeRuntimeError, _seed_models_cache

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
