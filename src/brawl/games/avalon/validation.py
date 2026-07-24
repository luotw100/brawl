"""Avalon 的新局与恢复边界。

新局只在这里读取 settings / registry 并摇定开局；落盘后的 ``meta.json`` 是此后续跑、
TTS 与回放的唯一真源。凡是会影响未来 prompt、模型选择或后处理的值都快照进 meta，
恢复路径不再读取活 YAML。
"""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from brawl.games.avalon import rules
from brawl.games.avalon.registry import Registry
from brawl.games.avalon.state import Role, Side
from brawl.games.run import RunSettings, SettingsError
from brawl.runtime import setup as core_setup
from brawl.runtime.files import UnsafePathError, read_regular_bytes
from brawl.runtime.journal import JournalCorruptionError, load_replay_sentences


class GameSetup(BaseModel):
    """一局已解析、可独立续跑的确定性方案。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    cast: tuple[str, ...]
    host_cast: str
    names: tuple[str, ...]
    host_name: str
    voices: tuple[str, ...]
    host_voice: str
    voice_designs: tuple[str, ...]
    host_voice_design: str
    tones: tuple[str, ...]
    strategies: tuple[str, ...]
    strategy_systems: tuple[str, ...]
    models: tuple[str, ...]
    opencode_models: tuple[str, ...]
    roles: tuple[Role, ...]
    first_leader: int
    lady_holder: int | None

    @model_validator(mode="after")
    def _check_shape(self) -> GameSetup:
        players = len(self.roles)
        if players not in rules.TEAM_SIZES:
            raise ValueError(
                f"setup.roles 必须是 {min(rules.TEAM_SIZES)}~{max(rules.TEAM_SIZES)} 人，收到 {players}"
            )
        per_seat = {
            "cast": self.cast,
            "names": self.names,
            "voices": self.voices,
            "voice_designs": self.voice_designs,
            "tones": self.tones,
            "strategies": self.strategies,
            "strategy_systems": self.strategy_systems,
            "models": self.models,
            "opencode_models": self.opencode_models,
        }
        wrong = {name: len(values) for name, values in per_seat.items() if len(values) != players}
        if wrong:
            raise ValueError(f"setup 逐座位列表长度必须都是 {players}：{wrong}")

        required_values = {
            "host_cast": self.host_cast,
            "host_name": self.host_name,
            "host_voice": self.host_voice,
            **{
                f"{name}[{index}]": value
                for name, values in per_seat.items()
                if name not in {"voice_designs", "tones", "opencode_models"}
                for index, value in enumerate(values, start=1)
            },
        }
        empty = [name for name, value in required_values.items() if not value.strip()]
        if empty:
            raise ValueError(f"setup 必填文本不能为空：{empty}")
        bad_refs = [ref for ref in self.opencode_models if ref and not _valid_model_ref(ref)]
        if bad_refs:
            raise ValueError(f"setup.opencode_models 必须是 providerID/modelID：{bad_refs}")

        if not 1 <= self.first_leader <= players:
            raise ValueError(f"setup.first_leader 必须在 1..{players}，收到 {self.first_leader}")
        if self.lady_holder is not None:
            expected_holder = (self.first_leader - 2) % players + 1
            if self.lady_holder != expected_holder:
                raise ValueError(
                    f"setup.lady_holder 必须是首任队长上家 {expected_holder}，收到 {self.lady_holder}"
                )

        _validate_role_pool(self.roles)
        return self

    @property
    def players(self) -> int:
        return len(self.roles)


class AvalonReplayMeta(BaseModel):
    """版本化 replay envelope；恢复入口只接受这个完整形状。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    game: Literal["avalon"] = "avalon"
    started_at: str
    backend: Literal["random", "opencode"]
    concurrency: int = Field(ge=1)
    render: bool
    seed: int | None
    setup: GameSetup
    model_labels: dict[str, str]

    @model_validator(mode="after")
    def _check_meta(self) -> AvalonReplayMeta:
        try:
            started_at = datetime.fromisoformat(self.started_at)
        except ValueError as exc:
            raise ValueError("started_at 不是 ISO-8601 时间") from exc
        if started_at.tzinfo is None:
            raise ValueError("started_at 必须携带时区")
        unknown = set(self.model_labels) - set(self.setup.models)
        if unknown:
            raise ValueError(f"model_labels 包含未入座模型：{sorted(unknown)}")
        if self.backend == "opencode" and any(
            not ref for ref in self.setup.opencode_models
        ):
            raise ValueError("opencode 对局的每个座位都必须冻结有效模型引用")
        return self


def resolve_and_validate(
    settings: RunSettings,
    registry: Registry,
    rng: random.Random,
) -> GameSetup:
    """把新局配置解析成无缺项 setup；所有活数据读取到此为止。"""
    try:
        board = rules.board_by_id(settings.board)
    except KeyError:
        raise SettingsError(f"未知阿瓦隆板子：{settings.board}（可用：{sorted(rules.BOARDS)}）") from None
    players = board.players
    cast = core_setup.resolve_personas(
        settings.cast,
        players=players,
        registry=registry,
        rng=rng,
        error=SettingsError,
    )
    host_cast = core_setup.resolve_host(
        settings.host_cast,
        registry=registry,
        default="herald",
        error=SettingsError,
    )
    strategies = core_setup.resolve_strategies(
        settings.strategies,
        players=players,
        registry=registry,
        rng=rng,
        error=SettingsError,
    )
    models = core_setup.resolve_models(
        settings.models,
        players=players,
        registry=registry,
        rng=rng,
        error=SettingsError,
    )
    core_setup.check_new_replay(settings.replay, error=SettingsError)
    first_leader = rng.randint(1, players)
    roles = list(board.roles)
    rng.shuffle(roles)
    opencode_models = tuple(registry.models[name].opencode_model or "" for name in models)
    if settings.backend == "opencode":
        unmapped = [name for name, ref in zip(models, opencode_models, strict=True) if not ref]
        if unmapped:
            raise SettingsError(f"模型缺 opencode_model 映射：{unmapped}（在 models.yaml 补上 providerID/modelID）")
    return GameSetup(
        cast=tuple(cast),
        host_cast=host_cast,
        names=tuple(registry.personas[pid].name for pid in cast),
        host_name=registry.hosts[host_cast].name,
        voices=tuple(registry.personas[pid].voice_id for pid in cast),
        host_voice=registry.hosts[host_cast].voice_id,
        voice_designs=tuple(registry.personas[pid].voice_design for pid in cast),
        host_voice_design=registry.hosts[host_cast].voice_design,
        tones=tuple(registry.personas[pid].tone for pid in cast),
        strategies=tuple(strategies),
        strategy_systems=tuple(registry.strategies[sid].system for sid in strategies),
        models=tuple(models),
        opencode_models=opencode_models,
        roles=tuple(roles),
        first_leader=first_leader,
        lady_holder=(first_leader - 2) % players + 1 if board.lady else None,
    )


def load_replay(replay: Path) -> AvalonReplayMeta:
    """在恢复开工前一次性验证目录、日志与 meta，错误统一成 SettingsError。"""
    sentences_path = replay / "sentences.jsonl"
    try:
        load_replay_sentences(replay)
    except (OSError, UnsafePathError, JournalCorruptionError) as exc:
        raise SettingsError(f"{sentences_path} 不是有效的 Sentence Runtime 日志：{exc}") from exc

    meta_path = replay / "meta.json"
    try:
        raw = read_regular_bytes(meta_path)
        return AvalonReplayMeta.model_validate_json(raw, strict=True)
    except (OSError, UnsafePathError, ValidationError, ValueError) as exc:
        raise SettingsError(f"{meta_path} 不是有效的 Avalon schema v1 meta：{exc}") from exc


def _validate_role_pool(roles: tuple[Role, ...]) -> None:
    players = len(roles)
    if roles.count(Role.MERLIN) != 1 or roles.count(Role.ASSASSIN) != 1:
        raise ValueError("setup.roles 必须恰有一名梅林和一名刺客")
    for unique in (Role.PERCIVAL, Role.MORGANA, Role.MORDRED, Role.OBERON):
        if roles.count(unique) > 1:
            raise ValueError(f"setup.roles 的特殊角色 {unique.value} 不能重复")
    if Role.MORGANA in roles and Role.PERCIVAL not in roles:
        raise ValueError("setup.roles 有莫甘娜时必须有派西维尔")
    evil = sum(role.side is Side.EVIL for role in roles)
    if evil != rules.EVIL_COUNT[players]:
        raise ValueError(
            f"setup.roles 的红方人数应为 {rules.EVIL_COUNT[players]}，收到 {evil}"
        )


def _valid_model_ref(ref: str) -> bool:
    provider, slash, model = ref.partition("/")
    return bool(
        provider
        and slash
        and model
        and not any(char.isspace() for char in ref)
    )
