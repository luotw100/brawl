"""狼人杀配置边界：settings + registry + rng -> 确定 GameSetup。"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from core import resume as resume_mod
from core import setup as core_setup
from werewolf import rules
from werewolf.registry import Registry
from werewolf.settings import SettingsError, WerewolfSettings
from werewolf.state import Role


@dataclass
class GameSetup:
    board: rules.BoardSpec
    cast: list[str]
    host_cast: str
    names: list[str]
    host_name: str
    voices: list[str]
    host_voice: str
    self_hosted_voices: list[str]
    host_self_hosted_voice: str
    voice_designs: list[str]
    host_voice_design: str
    strategies: list[str]
    models: list[str]
    roles: list[Role]

    def to_meta(self) -> dict:
        return {
            "board": self.board.id,
            "board_name": self.board.name,
            "cast": self.cast,
            "host_cast": self.host_cast,
            "names": self.names,
            "host_name": self.host_name,
            "voices": self.voices,
            "host_voice": self.host_voice,
            "self_hosted_voices": self.self_hosted_voices,
            "host_self_hosted_voice": self.host_self_hosted_voice,
            "voice_designs": self.voice_designs,
            "host_voice_design": self.host_voice_design,
            "strategies": self.strategies,
            "models": self.models,
            "roles": [r.value for r in self.roles],
        }


def resolve_and_validate(settings: WerewolfSettings, registry: Registry, rng: random.Random) -> GameSetup:
    board = rules.board_by_id(settings.board)
    cast = _resolve_personas(settings.cast, players=board.players, registry=registry, rng=rng)
    host_cast = core_setup.resolve_host(settings.host_cast, registry=registry, default="herald", error=SettingsError)
    strategies = core_setup.resolve_strategies(settings.strategies, players=board.players, registry=registry, rng=rng, error=SettingsError)
    models = core_setup.resolve_models(settings.models, players=board.players, registry=registry, rng=rng, error=SettingsError)
    core_setup.check_extractor(settings.extractor, registry=registry, error=SettingsError)
    _check_env(settings, models, registry)
    core_setup.check_new_replay(settings.replay, error=SettingsError)
    roles = list(board.roles)
    rng.shuffle(roles)
    return GameSetup(
        board=board,
        cast=cast,
        host_cast=host_cast,
        names=[registry.personas[pid].name for pid in cast],
        host_name=registry.hosts[host_cast].name,
        voices=[registry.personas[pid].voice_id for pid in cast],
        host_voice=registry.hosts[host_cast].voice_id,
        self_hosted_voices=[registry.personas[pid].self_hosted_voice_id for pid in cast],
        host_self_hosted_voice=registry.hosts[host_cast].self_hosted_voice_id,
        voice_designs=[registry.personas[pid].voice_design for pid in cast],
        host_voice_design=registry.hosts[host_cast].voice_design,
        strategies=strategies,
        models=models,
        roles=roles,
    )


def _check_env(settings: WerewolfSettings, models: list[str], registry: Registry) -> None:
    if settings.player != "llm":
        return
    core_setup.check_model_env([*models, settings.extractor], registry=registry, error=SettingsError)


def _resolve_personas(raw: list[str] | None, *, players: int, registry: Registry, rng: random.Random) -> list[str]:
    """狼人杀默认 12 人，当前共享玩家人设库可能不足 12 个；缺省时先无放回用满，再补复用。

    显式 --cast 仍逐个校验并保持用户给定顺序。这样一行 CLI 能直接跑 12 人场，同时把"是否
    允许复用人设"收在狼人杀边界，不改 core 的更严格默认。
    """
    if raw is not None:
        for pid in raw:
            if pid not in registry.personas:
                raise SettingsError(f"未知人设 id: {pid}（可选：{registry.persona_ids()}）")
        return raw
    ids = registry.persona_ids()
    if not ids:
        raise SettingsError("玩家人设库为空；请补充 cast.yaml 或显式指定 --cast")
    if len(ids) >= players:
        return rng.sample(ids, players)
    cast = rng.sample(ids, len(ids))
    cast.extend(rng.choice(ids) for _ in range(players - len(ids)))
    return cast


def validate_resume(settings: WerewolfSettings, registry: Registry) -> dict:
    run_dir = settings.replay
    assert run_dir is not None
    if not run_dir.exists():
        raise SettingsError(f"--resume 指向的对局目录不存在：{run_dir}")
    if not resume_mod.has_checkpoint(run_dir):
        raise SettingsError(f"{run_dir} 里没有 checkpoint 数据库，无法续跑")
    if resume_mod.last_watermark(run_dir) is None:
        raise SettingsError(f"{run_dir} 还没跑出任何可续跑的完整检查点；请重开新局而非续跑")
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        raise SettingsError(f"{run_dir} 缺 meta.json，无法续跑")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    _check_env(settings, meta["setup"]["models"], registry)
    return meta
