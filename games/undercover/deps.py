"""注入给 LangGraph 的依赖包（运行时通过 config["configurable"]["deps"] 取用）。

把"可替换的外部世界"（决策后端、随机源、配置、数据）从图里解耦出来，
换 --player 不动图。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from undercover.player import Player
from undercover.registry import Registry
from undercover.settings import UndercoverSettings


@dataclass
class Deps:
    settings: UndercoverSettings
    registry: Registry
    player: Player
    rng: random.Random
