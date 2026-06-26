"""配置的边界：把 raw settings + registry + rng 解析成一份**校验过的具体方案**，或抛错。

跑任何对局工作之前调一次 resolve_and_validate，它把"这一局到底是什么"一次性定死成
一个 GameSetup：哪个座位用什么人设、谁拿什么角色、首任队长是谁——**所有"开局设定"的
随机都在这里摇定**（依赖的只有 config+registry，开打前已全部就位），图里拿到的是确定值。
对局过程没有任何裁判侧随机（组队/表决/出牌全是玩家决策，平局规则也是确定的：
表决平票即否决），故 rng 只在这里用。

同时把所有"跨引用 / 世界一致性"约束查清：
    - cast / strategy / model 名是否在 registry、人设库是否够坐满
    - llm 后端下，用到的每个模型其 api_key_env 是否已配
    - --replay 目录是否已存在

通过之后，下游（_build_seats、pregame_node、open_recorder）默认拿到的就是干净正确、
且角色/队长已定的环境，不再各自重复校验或现场摇骰子——非法状态与"开局随机"都在边界
一次性解决（fail-fast + 确定性入图）。

为什么不放进 pydantic：这些约束依赖 registry（yaml 活数据）和文件系统，塞进 settings
会让最底层的 settings 反向依赖它们。pydantic 只管字段内在校验（players 范围、cast 长度，
见 settings.py 的 validator）；跨引用校验与开局摇牌归这里。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

from avalon.registry import Registry
from avalon.settings import AvalonSettings, SettingsError
from avalon.state import Role
from core import resume as resume_mod
from core import setup as core_setup


@dataclass
class GameSetup:
    """一局的确定性开局方案：边界解析+摇定。各列表按发牌顺序排列，下标 i 对应 i+1 号座位
    （座位号一律 1 起）。"""

    cast: list[str]  # 座位 → 人设 id
    host_cast: str  # 主持人人设 id（只供显示名与将来 TTS 音色；台词由代码合成）
    names: list[str]  # 座位 → 显示名（生成时从 cast.yaml 快照进来，回放层据此自圆，不再读 cast.yaml）
    host_name: str  # 主持人显示名（同上快照）
    voices: list[str]  # 座位 → TTS 音色 id（生成时从 cast.yaml 快照进来，空串=该人设未铸音色）
    host_voice: str  # 主持人 TTS 音色 id（同上快照，空串=未铸）
    strategies: list[str]  # 座位 → 流派 id
    models: list[str]  # 座位 → 逻辑模型名
    roles: list[Role]  # 座位 → 角色（已摇定）
    first_leader: int  # 首任队长座位号（1..N，已摇定）
    lady_holder: int | None  # 湖中仙女初始持有者（首任队长上家）；None=本局未启用

    def to_meta(self) -> dict:
        """落 replay meta.json 的 JSON 友好快照：这一局的确定性方案即复盘唯一真源
        （players/阵营计数是 roles 的派生量，不再单独存）。"""
        return {
            "cast": self.cast,
            "host_cast": self.host_cast,
            "names": self.names,
            "host_name": self.host_name,
            "voices": self.voices,
            "host_voice": self.host_voice,
            "strategies": self.strategies,
            "models": self.models,
            "roles": [r.value for r in self.roles],
            "first_leader": self.first_leader,
            "lady_holder": self.lady_holder,
        }


def resolve_and_validate(settings: AvalonSettings, registry: Registry, rng: random.Random) -> GameSetup:
    """把 settings 解析成确定的 GameSetup（座位号即下标）：cast 缺省即用 registry 默认
    补齐，角色/首任队长用 rng 摇定。违规抛 SettingsError（被 cli 捕获、只打印消息
    不甩 traceback）。"""
    cast = core_setup.resolve_unique_personas(settings.cast, players=settings.players, registry=registry, rng=rng, error=SettingsError)
    host_cast = core_setup.resolve_host(settings.host_cast, registry=registry, default="herald", error=SettingsError)
    strategies = core_setup.resolve_strategies(settings.strategies, players=settings.players, registry=registry, rng=rng, error=SettingsError)
    models = core_setup.resolve_models(settings.models, players=settings.players, registry=registry, rng=rng, error=SettingsError)
    core_setup.check_extractor(settings.extractor, registry=registry, error=SettingsError)
    _check_env(settings, models, registry)
    core_setup.check_new_replay(settings.replay, error=SettingsError)
    first_leader = rng.randint(1, settings.players)
    return GameSetup(
        cast=cast,
        host_cast=host_cast,
        # 显示名与音色都随开局快照进 meta：replay 据此自圆，cast.yaml 之后再改不影响老 replay。
        names=[registry.personas[pid].name for pid in cast],
        host_name=registry.hosts[host_cast].name,
        voices=[registry.personas[pid].voice_id for pid in cast],
        host_voice=registry.hosts[host_cast].voice_id,
        strategies=strategies,
        models=models,
        roles=_resolve_roles(settings, rng),
        first_leader=first_leader,
        # 官方规则：湖中仙女开局交给首任队长的上家（最晚轮到队长的人）
        lady_holder=(first_leader - 2) % settings.players + 1 if settings.lady else None,
    )



def _resolve_roles(settings: AvalonSettings, rng: random.Random) -> list[Role]:
    """按规则表与角色开关配出角色池，洗匀发给各座位。
    蓝方：梅林必上 + 可选派西维尔，余者忠臣；红方：刺客必上 + 可选莫甘娜/莫德雷德/奥伯伦，
    余者爪牙（开关与名额的自洽已由 settings 的形状校验保证）。"""
    good = [Role.MERLIN] + ([Role.PERCIVAL] if settings.percival else [])
    evil = [Role.ASSASSIN] + [r for r, on in ((Role.MORGANA, settings.morgana), (Role.MORDRED, settings.mordred), (Role.OBERON, settings.oberon)) if on]
    pool = good + [Role.SERVANT] * (settings.good - len(good)) + evil + [Role.MINION] * (settings.evil - len(evil))
    rng.shuffle(pool)
    return pool


def _check_env(settings: AvalonSettings, models: list[str], registry: Registry) -> None:
    """llm 后端下，用到的每个模型（含 extractor）其 api_key_env 必须已配。"""
    if settings.player != "llm":
        return
    core_setup.check_model_env([*models, settings.extractor], registry=registry, error=SettingsError)



def validate_resume(settings: AvalonSettings, registry: Registry) -> dict:
    """续跑的边界校验（与 resolve_and_validate 对称，但不重摇开局——开局方案与对局状态都
    从 checkpoint/meta 恢复，绝不再掷骰子）。查清：目录在、有 checkpoint 与可续跑水位、
    meta 可读、用到的模型 key 都已配。返回解析好的 meta（含开局方案，供编排层取用）。

    replay 已存在由 settings 形状校验保证（--resume 必带 --replay）；这里查"世界一致性"。"""
    run_dir = settings.replay
    assert run_dir is not None  # settings._check 已保证 --resume 必带 --replay
    if not run_dir.exists():
        raise SettingsError(f"--resume 指向的对局目录不存在：{run_dir}")
    if not resume_mod.has_checkpoint(run_dir):
        raise SettingsError(f"{run_dir} 里没有 checkpoint 数据库，无法续跑（它可能不是续跑产出的对局）")
    if resume_mod.last_watermark(run_dir) is None:
        raise SettingsError(f"{run_dir} 还没跑出任何可续跑的完整检查点；请重开新局而非续跑")
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        raise SettingsError(f"{run_dir} 缺 meta.json，无法续跑")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # 开局已定，模型清单从 meta 取（不是从当前 settings——续跑要忠于原局）；只查它们的 key 在不在。
    _check_env(settings, meta["setup"]["models"], registry)
    return meta
