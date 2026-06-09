"""配置的边界：把 raw settings + registry + rng 解析成一份**校验过的具体方案**，或抛错。

跑任何对局工作之前调一次 resolve_and_validate，它把"这一局到底是什么"一次性定死成
一个 GameSetup：哪个座位用什么人设/模型、谁是卧底、用哪对词——**所有"开局设定"的
随机都在这里摇定**（依赖的只有 config+registry，开打前已全部就位），图里拿到的是确定值。
对照之下，平票淘汰那种依赖运行期票数的随机才留在节点里（tally_node）。

同时把所有"跨引用 / 世界一致性"约束查清：
    - cast id / model 名是否在 registry
    - 人设库是否够坐满
    - llm 后端下，用到的每个模型其 api_key_env 是否已配
    - --replay 目录是否已存在

通过之后，下游（_build_seats、pregame_node、open_recorder、LLMPlayer._model）默认拿到的
就是干净正确、且身份/词已定的环境，不再各自重复校验或现场摇骰子——非法状态与"开局随机"
都在边界一次性解决（fail-fast + 确定性入图）。

为什么不放进 pydantic：这些约束依赖 registry（yaml 活数据）和文件系统/环境变量，
塞进 settings 会让最底层的 settings 反向依赖它们。pydantic 只管字段内在校验（值是否
良构，见 settings.py 的 validator）；跨引用校验与开局摇牌归这里。
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from itertools import cycle, islice

from undercover.registry import Registry
from undercover.settings import SettingsError, UndercoverSettings
from undercover.state import Role


@dataclass
class GameSetup:
    """一局的确定性开局方案：边界解析+摇定，按座位号下标对齐（长度均为 players）。"""

    cast: list[str]  # 座位 → 人设 id
    strategies: list[str]  # 座位 → 流派 id
    models: list[str]  # 座位 → 逻辑模型名
    roles: list[Role]  # 座位 → 身份（已摇定）
    word_pair: tuple[str, str]  # (平民词, 卧底词)（已摇定，含方向）

    def to_meta(self) -> dict:
        """落 replay meta.json 的 JSON 友好快照：这一局的确定性方案即复盘唯一真源
        （players/undercover 等计数是 roles 的派生量，不再单独存）。"""
        return {
            "cast": self.cast,
            "strategies": self.strategies,
            "models": self.models,
            "roles": [r.value for r in self.roles],
            "word_pair": list(self.word_pair),
        }


def resolve_and_validate(settings: UndercoverSettings, registry: Registry, rng: random.Random) -> GameSetup:
    """把 settings 解析成确定的 GameSetup（座位号即下标）：cast/models 缺省即用 registry
    默认补齐，身份/词缺省即用 rng 摇定。违规抛 SettingsError（被 cli 捕获、只打印消息
    不甩 traceback）。"""
    cast = _resolve_cast(settings, registry)
    strategies = _resolve_strategies(settings, registry)
    models = _resolve_models(settings, registry)
    _check_env(settings, models, registry)
    _check_replay(settings)
    return GameSetup(
        cast=cast,
        strategies=strategies,
        models=models,
        roles=_resolve_roles(settings, rng),
        word_pair=_resolve_word_pair(settings, registry, rng),
    )


def _resolve_cast(settings: UndercoverSettings, registry: Registry) -> list[str]:
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


def _resolve_strategies(settings: UndercoverSettings, registry: Registry) -> list[str]:
    """显式 --strategies 则逐个校验在册；否则全员 standard（流派的中性基线，必须在册）。"""
    if settings.strategies is not None:
        for sid in settings.strategies:
            if sid not in registry.strategies:
                raise SettingsError(f"未知流派 id: {sid}（可选：{registry.strategy_ids()}）")
        return settings.strategies
    if "standard" not in registry.strategies:
        raise SettingsError("strategies.yaml 缺少默认流派 standard；请补上或显式指定 --strategies")
    return ["standard"] * settings.players


def _resolve_models(settings: UndercoverSettings, registry: Registry) -> list[str]:
    """显式 --models 则逐个校验在册；否则在 provider 池里循环分配，座位多于模型数也能填满。"""
    if settings.models is not None:
        for name in settings.models:
            if name not in registry.models:
                raise SettingsError(f"未知模型: {name}（可选：{registry.model_names()}）")
        return settings.models
    return list(islice(cycle(registry.model_names()), settings.players))


def _resolve_roles(settings: UndercoverSettings, rng: random.Random) -> list[Role]:
    """随机抽 undercover 个座位当卧底，其余平民（blank 已被 settings 校验定为 0）。"""
    order = list(range(settings.players))
    rng.shuffle(order)
    undercover_seats = set(order[: settings.undercover])
    return [Role.UNDERCOVER if no in undercover_seats else Role.CIVILIAN for no in range(settings.players)]


def _resolve_word_pair(settings: UndercoverSettings, registry: Registry, rng: random.Random) -> tuple[str, str]:
    """显式 --word-pair 则照用；否则从词库随机抽一对、方向也随机。返回 (平民词, 卧底词)。"""
    return settings.word_pair or registry.random_word_pair(rng)


def _check_env(settings: UndercoverSettings, models: list[str], registry: Registry) -> None:
    """llm 后端下，用到的每个模型其 api_key_env 必须已配——前置在这里查，而不是等
    跑到一半某席位首次调用模型时才炸。random 后端不调模型，跳过。"""
    if settings.player != "llm":
        return
    for name in dict.fromkeys(models):  # 去重保序
        spec = registry.models[name]
        if not os.getenv(spec.api_key_env):
            raise SettingsError(f"环境变量 {spec.api_key_env} 未设置（模型 {spec.model_id} 需要它）；请在 .env 配置")


def _check_replay(settings: UndercoverSettings) -> None:
    """--replay 目录已存在即报错（不覆盖、无 --force）：要重跑换个名。"""
    if settings.replay is not None and settings.replay.exists():
        raise SettingsError(f"--replay 目录已存在，拒绝覆盖：{settings.replay}（换个名，或先删掉它）")
