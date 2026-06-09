"""共享测试夹具。

只放真正跨文件复用的东西：真实 registry（读 data/*.yaml）。其余构造按需在各测试里就地写，
保持每个测试自解释。所有测试都走 --player random / 纯函数，**零 key、零网络**。
"""

from __future__ import annotations

import pytest

from undercover.registry import Registry, load_registry


@pytest.fixture
def registry() -> Registry:
    """data/ 下三份 yaml 的内存视图（人设库/模型池/词库）。"""
    return load_registry()
