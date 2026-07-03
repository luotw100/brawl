"""跨玩法配置边界解析工具。"""

from __future__ import annotations

import os
import random
from pathlib import Path

from core.registry import Registry

_SettingsError = type[Exception]

_MODEL_REPEAT_DECAY = 0.5


def resolve_unique_personas(raw: list[str] | None, *, players: int, registry: Registry, rng: random.Random, error: _SettingsError) -> list[str]:
    """显式列表逐个校验；缺省从玩家人设池无放回抽样。"""
    if raw is not None:
        for pid in raw:
            if pid not in registry.personas:
                raise error(f"未知人设 id: {pid}（可选：{registry.persona_ids()}）")
        return raw
    ids = registry.persona_ids()
    if len(ids) < players:
        raise error(f"玩家人设只有 {len(ids)} 个，不够 {players} 个座位；请用 --cast 指定或补充 cast.yaml")
    return rng.sample(ids, players)


def resolve_host(raw: str | None, *, registry: Registry, default: str, error: _SettingsError) -> str:
    """显式主持人校验在册；缺省要求默认主持人在册。"""
    if raw is not None:
        if raw not in registry.hosts:
            raise error(f"未知主持人人设 id: {raw}（可选：{registry.host_ids()}）")
        return raw
    if default not in registry.hosts:
        raise error(f"hosts.yaml 缺少默认主持人人设 {default}；请补上或显式指定 --host-cast")
    return default


def resolve_strategies(raw: list[str] | None, *, players: int, registry: Registry, rng: random.Random, error: _SettingsError) -> list[str]:
    """显式流派逐个校验；缺省逐座位从流派库放回抽样。"""
    if raw is not None:
        for sid in raw:
            if sid not in registry.strategies:
                raise error(f"未知流派 id: {sid}（可选：{registry.strategy_ids()}）")
        return raw
    sids = registry.strategy_ids()
    if not sids:
        raise error("strategies.yaml 没有任何流派；请补上至少一个或显式指定 --strategies")
    return [rng.choice(sids) for _ in range(players)]


def resolve_models(
    raw: list[str] | None,
    *,
    players: int,
    registry: Registry,
    rng: random.Random,
    error: _SettingsError,
) -> list[str]:
    """显式模型逐个校验；缺省从模型池有放回降权抽样。"""
    if raw is not None:
        for name in raw:
            if name not in registry.models:
                raise error(f"未知模型: {name}（可选：{registry.model_names()}）")
        return raw
    names = registry.model_names()
    counts = dict.fromkeys(names, 0)
    picked: list[str] = []
    for _ in range(players):
        weights = [_MODEL_REPEAT_DECAY ** counts[name] for name in names]
        name = rng.choices(names, weights=weights, k=1)[0]
        picked.append(name)
        counts[name] += 1
    return picked


def check_extractor(name: str, *, registry: Registry, error: _SettingsError) -> None:
    if name not in registry.models:
        raise error(f"未知 extractor 模型: {name}（可选：{registry.model_names()}）")


def check_model_env(names: list[str], *, registry: Registry, error: _SettingsError) -> None:
    """用到的每个模型 api_key_env 必须在开工前就绪。"""
    for name in dict.fromkeys(names):
        spec = registry.models[name]
        if not os.getenv(spec.api_key_env):
            raise error(f"环境变量 {spec.api_key_env} 未设置（模型 {spec.model_id} 需要它）；请在 .env 配置")


def check_new_replay(path: Path | None, *, error: _SettingsError) -> None:
    if path is not None and path.exists():
        raise error(f"--replay 目录已存在，拒绝覆盖：{path}（换个名，或先删掉它）")
