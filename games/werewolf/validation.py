"""狼人杀配置边界：把 raw settings + registry + rng 解析成一份**校验过的具体方案**，或抛错。

与 avalon/validation.py 同构：resolve_and_validate 把"这一局到底是什么"一次性定死成
GameSetup——哪个座位用什么人设、谁拿什么角色——**所有"开局设定"的随机都在这里摇定**
（角色洗牌是唯一的开局随机）。对局过程没有任何裁判侧随机（刀口多数决、平票无人出局
都是确定规则），故 rng 只在这里用。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from core import setup as core_setup
from werewolf import rules
from werewolf.registry import Registry
from werewolf.rules import Role
from werewolf.settings import SettingsError, WerewolfSettings


@dataclass
class GameSetup:
    """一局的确定性开局方案：边界解析+摇定。各列表按发牌顺序排列，下标 i 对应 i+1 号座位
    （座位号一律 1 起）。"""

    board: str  # 板子 id（角色池/人数由 rules.board_by_id 派生）
    cast: list[str]  # 座位 → 人设 id（12 人局允许复用人设，见 core_setup.resolve_personas）
    host_cast: str
    names: list[str]  # 座位 → 显示名（生成时从 cast.yaml 快照进来，回放层据此自圆）
    host_name: str
    voices: list[str]  # 座位 → TTS 音色 id（同上快照，空串=未铸）
    host_voice: str
    self_hosted_voices: list[str]
    host_self_hosted_voice: str
    voice_designs: list[str]
    host_voice_design: str
    strategies: list[str]  # 座位 → 流派 id
    models: list[str]  # 座位 → 逻辑模型名
    roles: list[Role]  # 座位 → 角色（已摇定）

    def to_meta(self) -> dict:
        """落 replay meta.json 的 JSON 友好快照：这一局的确定性方案即复盘唯一真源。"""
        return {
            "board": self.board,
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
            # 终端/回放渲染的席位配色（数据驱动，渲染层不硬编码玩法角色→颜色）。
            "role_colors": {r.value: ("red" if r is Role.WEREWOLF else "blue") for r in set(self.roles)},
        }


def resolve_and_validate(settings: WerewolfSettings, registry: Registry, rng: random.Random) -> GameSetup:
    """把 settings 解析成确定的 GameSetup。违规抛 SettingsError（被 cli 捕获、只打印消息）。"""
    board = rules.board_by_id(settings.board)
    # 狼人杀 12 人局允许复用人设：共享人设库可能不足 12 个，缺省采样先无放回用满再补复用。
    cast = core_setup.resolve_personas(
        settings.cast, players=board.players, registry=registry, rng=rng, error=SettingsError, allow_repeat=True,
    )
    host_cast = core_setup.resolve_host(settings.host_cast, registry=registry, default="herald", error=SettingsError)
    strategies = core_setup.resolve_strategies(settings.strategies, players=board.players, registry=registry, rng=rng, error=SettingsError)
    models = core_setup.resolve_models(settings.models, players=board.players, registry=registry, rng=rng, error=SettingsError)
    core_setup.check_new_replay(settings.replay, error=SettingsError)
    roles = list(board.roles)
    rng.shuffle(roles)
    return GameSetup(
        board=board.id,
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


def validate_resume(settings: WerewolfSettings, registry: Registry) -> dict:
    """续跑的边界校验：目录在、有句子日志、meta 可读。返回解析好的 meta。"""
    run_dir = settings.replay
    assert run_dir is not None  # settings._check 已保证 --resume 必带 --replay
    if not run_dir.exists():
        raise SettingsError(f"--resume 指向的对局目录不存在：{run_dir}")
    sentences_path = run_dir / "sentences.jsonl"
    if not sentences_path.exists():
        raise SettingsError(f"{run_dir} 里没有 sentences.jsonl，无法续跑")
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        raise SettingsError(f"{run_dir} 缺 meta.json，无法续跑")
    return json.loads(meta_path.read_text(encoding="utf-8"))
