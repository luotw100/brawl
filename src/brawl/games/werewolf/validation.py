"""狼人杀开局与续跑边界。

fresh 把 settings + registry + rng 一次性解成不可变 ``GameSetup``；
resume 只从 replay 读取版本化 ``ReplayMeta``。下游不再查活 registry，
因此中断后修改 YAML 不会改变本局 prompt、模型或 TTS 元信息。
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from brawl.games.run import RunSettings, SettingsError
from brawl.games.werewolf import rules
from brawl.games.werewolf.registry import Registry
from brawl.games.werewolf.rules import Role
from brawl.runtime import setup as core_setup
from brawl.runtime.files import UnsafePathError, read_regular_bytes
from brawl.runtime.journal import JournalCorruptionError, load_replay_sentences

NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class GameSetup(BaseModel):
    """一局已解析、已摇定、可独立续跑的完整开局快照。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    board: NonEmptyStr
    cast: tuple[NonEmptyStr, ...]
    host_cast: NonEmptyStr
    names: tuple[NonEmptyStr, ...]
    host_name: NonEmptyStr
    voices: tuple[NonEmptyStr, ...]
    host_voice: NonEmptyStr
    voice_designs: tuple[str, ...]
    host_voice_design: str
    tones: tuple[str, ...]
    strategies: tuple[NonEmptyStr, ...]
    strategy_systems: tuple[NonEmptyStr, ...]
    models: tuple[NonEmptyStr, ...]
    opencode_models: tuple[str, ...]
    roles: tuple[Role, ...]

    @model_validator(mode="after")
    def _validate_board_and_seats(self) -> GameSetup:
        try:
            board = rules.board_by_id(self.board)
        except KeyError as exc:
            raise ValueError(f"未知狼人杀板子: {self.board}") from exc

        seat_fields = (
            "cast",
            "names",
            "voices",
            "voice_designs",
            "tones",
            "strategies",
            "strategy_systems",
            "models",
            "opencode_models",
            "roles",
        )
        bad = [
            f"{field}={len(getattr(self, field))}"
            for field in seat_fields
            if len(getattr(self, field)) != board.players
        ]
        if bad:
            raise ValueError(
                f"setup 座位列表长度必须全部等于 {board.players}: " + "，".join(bad)
            )
        if Counter(self.roles) != Counter(board.roles):
            expected = sorted(role.value for role in board.roles)
            actual = sorted(role.value for role in self.roles)
            raise ValueError(f"setup.roles 与板子角色池不一致：期望 {expected}，收到 {actual}")
        invalid_model_refs = [
            ref
            for ref in self.opencode_models
            if ref and not _valid_model_ref(ref)
        ]
        if invalid_model_refs:
            raise ValueError(
                "setup.opencode_models 必须是 providerID/modelID: "
                f"{invalid_model_refs}"
            )
        return self

class ReplayMeta(BaseModel):
    """Werewolf replay ``meta.json`` schema；只支持当前版本。"""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    game: Literal["werewolf"] = "werewolf"
    started_at: datetime
    backend: Literal["random", "opencode"]
    concurrency: int = Field(ge=1)
    render: bool
    seed: int | None
    setup: GameSetup
    board_name: NonEmptyStr
    model_labels: dict[str, str]

    @model_validator(mode="after")
    def _validate_derived_fields(self) -> ReplayMeta:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("started_at 必须携带时区")
        board = rules.board_by_id(self.setup.board)
        if self.board_name != board.name:
            raise ValueError(
                f"board_name 与 setup.board 不一致：期望 {board.name!r}，收到 {self.board_name!r}"
            )
        unknown_labels = sorted(set(self.model_labels) - set(self.setup.models))
        if unknown_labels:
            raise ValueError(f"model_labels 包含本局未使用的模型: {unknown_labels}")
        if self.backend == "opencode":
            missing = [
                seat
                for seat, model_ref in enumerate(self.setup.opencode_models, start=1)
                if not model_ref
            ]
            if missing:
                raise ValueError(f"OpenCode 对局缺少已冻结的座位模型引用: {missing}")
        return self

def resolve_and_validate(
    settings: RunSettings,
    registry: Registry,
    rng: random.Random,
) -> GameSetup:
    """解析 fresh 配置；违规统一抛 ``SettingsError``。"""
    board = rules.board_by_id(settings.board)
    cast = core_setup.resolve_personas(
        settings.cast,
        players=board.players,
        registry=registry,
        rng=rng,
        error=SettingsError,
        allow_repeat=True,
    )
    host_cast = core_setup.resolve_host(
        settings.host_cast,
        registry=registry,
        default="herald",
        error=SettingsError,
    )
    strategies = core_setup.resolve_strategies(
        settings.strategies,
        players=board.players,
        registry=registry,
        rng=rng,
        error=SettingsError,
    )
    models = core_setup.resolve_models(
        settings.models,
        players=board.players,
        registry=registry,
        rng=rng,
        error=SettingsError,
    )
    core_setup.check_new_replay(settings.replay, error=SettingsError)

    roles = list(board.roles)
    rng.shuffle(roles)
    effective_models = tuple(
        registry.models[name].opencode_model or "" for name in models
    )
    try:
        setup = GameSetup(
            board=board.id,
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
            opencode_models=effective_models,
            roles=tuple(roles),
        )
    except ValidationError as exc:
        raise SettingsError(f"无效的狼人杀开局配置: {exc}") from exc
    if settings.backend == "opencode":
        unmapped = [
            name
            for name, ref in zip(setup.models, setup.opencode_models, strict=True)
            if not ref
        ]
        if unmapped:
            raise SettingsError(f"模型缺 opencode_model 映射: {unmapped}（在 models.yaml 补上 providerID/modelID）")
    return setup


def build_replay_meta(
    settings: RunSettings,
    registry: Registry,
    setup: GameSetup,
    *,
    started_at: datetime,
) -> ReplayMeta:
    """用已校验 setup 构造唯一 replay envelope。"""
    board = rules.board_by_id(setup.board)
    try:
        return ReplayMeta(
            started_at=started_at,
            backend=settings.backend,
            concurrency=settings.concurrency,
            render=settings.render,
            seed=settings.seed,
            setup=setup,
            board_name=board.name,
            model_labels={
                name: registry.models[name].display
                for name in dict.fromkeys(setup.models)
                if registry.models[name].display
            },
        )
    except ValidationError as exc:  # 只会由程序内部违反 fresh 边界不变量触发
        raise SettingsError(f"无效的狼人杀 replay 元信息: {exc}") from exc


def load_replay_meta(run_dir: Path) -> ReplayMeta:
    """校验续跑目录并严格解析 ``meta.json``。"""
    sentences_path = run_dir / "sentences.jsonl"
    try:
        load_replay_sentences(run_dir)
    except (OSError, UnsafePathError, JournalCorruptionError) as exc:
        raise SettingsError(
            f"{sentences_path} 不是有效的 Sentence Runtime 日志：{exc}"
        ) from exc

    meta_path = run_dir / "meta.json"
    try:
        raw = read_regular_bytes(meta_path)
        return ReplayMeta.model_validate_json(raw, strict=True)
    except (OSError, UnsafePathError, ValidationError, ValueError) as exc:
        raise SettingsError(f"{meta_path} 不是有效的 Werewolf replay meta: {exc}") from exc


def _valid_model_ref(ref: str) -> bool:
    provider, slash, model = ref.partition("/")
    return bool(
        provider
        and slash
        and model
        and not any(char.isspace() for char in ref)
    )
