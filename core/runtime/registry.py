"""跨玩法可调内容注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel

from core.runtime.models import ModelSpec


class Persona(BaseModel):
    """一个人设：只管怎么说话，不绑定模型。"""

    id: str
    name: str
    tone: str = ""
    voice_id: str = ""
    voice_design: str = ""


class Strategy(BaseModel):
    """一个流派：只管怎么赢，不绑定人设与模型。"""

    id: str
    name: str
    system: str


@dataclass
class Registry:
    """data/ 下通用内容的内存视图。"""

    models: dict[str, ModelSpec]
    personas: dict[str, Persona]
    hosts: dict[str, Persona]
    strategies: dict[str, Strategy]
    avatars_dir: Path

    def avatar_path(self, model_name: str | None) -> Path | None:
        spec = self.models.get(model_name) if model_name is not None else None
        if spec is None or not spec.avatar:
            return None
        path = self.avatars_dir / f"{spec.avatar}.png"
        return path if path.is_file() else None

    def model_names(self) -> list[str]:
        return list(self.models)

    def persona_ids(self) -> list[str]:
        return list(self.personas)

    def host_ids(self) -> list[str]:
        return list(self.hosts)

    def strategy_ids(self) -> list[str]:
        return list(self.strategies)


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_registry(data_dir: Path) -> Registry:
    """加载通用 models/cast/hosts/strategies 四轴内容。"""
    models_raw = _read_yaml(data_dir / "models.yaml")
    cast_raw = _read_yaml(data_dir / "cast.yaml")
    hosts_raw = _read_yaml(data_dir / "hosts.yaml")
    strategies_raw = _read_yaml(data_dir / "strategies.yaml")

    models = {m["name"]: ModelSpec(**m) for m in models_raw["models"]}
    personas = {p["id"]: Persona(**p) for p in cast_raw["personas"]}
    hosts = {h["id"]: Persona(**h) for h in hosts_raw["hosts"]}
    strategies = {s["id"]: Strategy(**s) for s in strategies_raw["strategies"]}
    return Registry(models=models, personas=personas, hosts=hosts, strategies=strategies, avatars_dir=data_dir / "avatars")
