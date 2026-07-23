"""跨玩法配置边界解析工具。"""

from __future__ import annotations

import random
from pathlib import Path

from core.runtime.registry import Registry

_SettingsError = type[Exception]

_MODEL_REPEAT_DECAY = 0.5


def resolve_personas(
    raw: list[str] | None,
    *,
    players: int,
    registry: Registry,
    rng: random.Random,
    error: _SettingsError,
    allow_repeat: bool = False,
) -> list[str]:
    """显式列表逐个校验；缺省从玩家人设池抽样。

    allow_repeat=False（默认，阿瓦隆）：无放回抽样，各座位人设互异（免撞音色/网名）。
    allow_repeat=True（狼人杀 12 人局）：人设池不足座位数时先无放回用满、再有放回补复用；
    池子够大则与默认模式一样无放回。显式 --cast 不受此开关影响（用户给什么就是什么）。
    """
    if raw is not None:
        for pid in raw:
            if pid not in registry.personas:
                raise error(f"未知人设 id: {pid}（可选：{registry.persona_ids()}）")
        return raw
    ids = registry.persona_ids()
    if len(ids) < players:
        if not allow_repeat:
            raise error(f"玩家人设只有 {len(ids)} 个，不够 {players} 个座位；请用 --cast 指定或补充 cast.yaml")
        if not ids:
            raise error("玩家人设库为空；请补充 cast.yaml 或显式指定 --cast")
        cast = rng.sample(ids, len(ids))
        cast.extend(rng.choice(ids) for _ in range(players - len(ids)))
        return cast
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


def check_new_replay(path: Path | None, *, error: _SettingsError) -> None:
    if path is not None and path.exists():
        raise error(f"--replay 目录已存在，拒绝覆盖：{path}（换个名，或先删掉它）")
