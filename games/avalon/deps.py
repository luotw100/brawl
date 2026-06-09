"""注入给 LangGraph 的依赖包（运行时通过 config["configurable"]["deps"] 取用）。

把"可替换的外部世界"（决策后端、配置、数据）从图里解耦出来，换 --player 不动图。
阿瓦隆的对局过程没有裁判侧随机（开局摇牌已前置到边界），故这里不带 rng。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from avalon.host import Host
from avalon.player import Player
from avalon.registry import Registry
from avalon.settings import AvalonSettings


@dataclass
class Deps:
    settings: AvalonSettings
    registry: Registry
    player: Player
    # 主持人不是可替换后端、无配置项（persona 只影响 meta 里的显示名），默认即真身。
    host: Host = field(default_factory=Host)
