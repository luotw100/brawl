"""Avalon 可调内容注册表。

通用 models/cast/hosts/strategies 加载逻辑在 core.registry；这里保留 Avalon 的 DATA_DIR
和 load_registry() 入口，让玩法代码不关心目录细节。
"""

from __future__ import annotations

from pathlib import Path

from core.models import ModelSpec
from core.registry import Persona, Registry, Strategy
from core.registry import load_registry as load_common_registry

DATA_DIR = Path(__file__).parent / "data"


def load_registry(data_dir: Path = DATA_DIR) -> Registry:
    return load_common_registry(data_dir)


__all__ = ["DATA_DIR", "ModelSpec", "Persona", "Registry", "Strategy", "load_registry"]
