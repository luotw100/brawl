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

import os
import random
from dataclasses import dataclass


from avalon.registry import Registry
from avalon.settings import AvalonSettings, SettingsError
from avalon.state import Role


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
    cast = _resolve_cast(settings, registry)
    host_cast = _resolve_host_cast(settings, registry)
    strategies = _resolve_strategies(settings, registry)
    models = _resolve_models(settings, registry, rng)
    _check_extractor(settings, registry)
    _check_env(settings, models, registry)
    _check_replay(settings)
    first_leader = rng.randint(1, settings.players)
    return GameSetup(
        cast=cast,
        host_cast=host_cast,
        # 显示名与音色都随开局快照进 meta：replay 据此自圆，cast.yaml 之后再改不影响老 replay。
        names=[registry.personas[pid].name for pid in cast],
        host_name=registry.personas[host_cast].name,
        voices=[registry.personas[pid].voice_id for pid in cast],
        host_voice=registry.personas[host_cast].voice_id,
        strategies=strategies,
        models=models,
        roles=_resolve_roles(settings, rng),
        first_leader=first_leader,
        # 官方规则：湖中仙女开局交给首任队长的上家（最晚轮到队长的人）
        lady_holder=(first_leader - 2) % settings.players + 1 if settings.lady else None,
    )


def _resolve_cast(settings: AvalonSettings, registry: Registry) -> list[str]:
    """显式 --cast 则逐个校验在册；否则取人设库前 N 个（不够即报错）。"""
    if settings.cast is not None:
        for pid in settings.cast:
            if pid not in registry.personas:
                raise SettingsError(f"未知人设 id: {pid}（可选：{registry.persona_ids()}）")
        return settings.cast
    ids = registry.persona_ids()
    if len(ids) < settings.players:
        raise SettingsError(f"人设库只有 {len(ids)} 个，不够 {settings.players} 个座位；请用 --cast 指定或补充 cast.yaml")
    return ids[: settings.players]


def _resolve_host_cast(settings: AvalonSettings, registry: Registry) -> str:
    """显式 --host-cast 则校验在册；否则用默认主持人 herald（必须在册）。"""
    if settings.host_cast is not None:
        if settings.host_cast not in registry.personas:
            raise SettingsError(f"未知主持人人设 id: {settings.host_cast}（可选：{registry.persona_ids()}）")
        return settings.host_cast
    if "herald" not in registry.personas:
        raise SettingsError("cast.yaml 缺少默认主持人人设 herald；请补上或显式指定 --host-cast")
    return "herald"


def _resolve_strategies(settings: AvalonSettings, registry: Registry) -> list[str]:
    """显式 --strategies 则逐个校验在册；否则全员 standard（流派的中性基线，必须在册）。"""
    if settings.strategies is not None:
        for sid in settings.strategies:
            if sid not in registry.strategies:
                raise SettingsError(f"未知流派 id: {sid}（可选：{registry.strategy_ids()}）")
        return settings.strategies
    if "standard" not in registry.strategies:
        raise SettingsError("strategies.yaml 缺少默认流派 standard；请补上或显式指定 --strategies")
    return ["standard"] * settings.players


def _resolve_models(settings: AvalonSettings, registry: Registry, rng: random.Random) -> list[str]:
    """显式 --models 则逐个校验在册；否则从 provider 池里随机分配（放回抽样，可重复）。"""
    if settings.models is not None:
        for name in settings.models:
            if name not in registry.models:
                raise SettingsError(f"未知模型: {name}（可选：{registry.model_names()}）")
        return settings.models
    names = registry.model_names()
    return [rng.choice(names) for _ in range(settings.players)]


def _resolve_roles(settings: AvalonSettings, rng: random.Random) -> list[Role]:
    """按规则表与角色开关配出角色池，洗匀发给各座位。
    蓝方：梅林必上 + 可选派西维尔，余者忠臣；红方：刺客必上 + 可选莫甘娜/莫德雷德/奥伯伦，
    余者爪牙（开关与名额的自洽已由 settings 的形状校验保证）。"""
    good = [Role.MERLIN] + ([Role.PERCIVAL] if settings.percival else [])
    evil = [Role.ASSASSIN] + [r for r, on in ((Role.MORGANA, settings.morgana), (Role.MORDRED, settings.mordred), (Role.OBERON, settings.oberon)) if on]
    pool = good + [Role.SERVANT] * (settings.good - len(good)) + evil + [Role.MINION] * (settings.evil - len(evil))
    rng.shuffle(pool)
    return pool


def _check_extractor(settings: AvalonSettings, registry: Registry) -> None:
    """extractor 引用的逻辑模型必须在册（配置静态约束，与后端无关）。"""
    if settings.extractor not in registry.models:
        raise SettingsError(f"未知 extractor 模型: {settings.extractor}（可选：{registry.model_names()}）")


def _check_env(settings: AvalonSettings, models: list[str], registry: Registry) -> None:
    """llm 后端下，用到的每个模型（含 extractor）其 api_key_env 必须已配——前置在这里查，
    而不是等跑到一半某席位首次调用模型时才炸。random 后端不调模型，跳过。"""
    if settings.player != "llm":
        return
    for name in dict.fromkeys([*models, settings.extractor]):  # 去重保序
        spec = registry.models[name]
        if not os.getenv(spec.api_key_env):
            raise SettingsError(f"环境变量 {spec.api_key_env} 未设置（模型 {spec.model_id} 需要它）；请在 .env 配置")


def _check_replay(settings: AvalonSettings) -> None:
    """--replay 目录已存在即报错（不覆盖、无 --force）：要重跑换个名。"""
    if settings.replay is not None and settings.replay.exists():
        raise SettingsError(f"--replay 目录已存在，拒绝覆盖：{settings.replay}（换个名，或先删掉它）")
