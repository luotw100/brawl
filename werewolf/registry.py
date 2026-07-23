"""狼人杀可调内容注册表入口。

狼人杀拥有自己的 data 目录。两个玩法不能在运行时互相引用对方的数据文件；后续调人设、
模型池、流派时各改各的。
"""

from __future__ import annotations

from pathlib import Path

from core.runtime.models import ModelSpec
from core.runtime.registry import Persona, Registry, Strategy
from core.runtime.registry import load_registry as load_common_registry

DATA_DIR = Path(__file__).parent / "data"


def load_registry(data_dir: Path = DATA_DIR) -> Registry:
    return load_common_registry(data_dir)


__all__ = ["DATA_DIR", "ModelSpec", "Persona", "Registry", "Strategy", "load_registry"]
