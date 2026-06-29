"""注入给狼人杀 LangGraph 的运行时依赖。"""

from __future__ import annotations

from dataclasses import dataclass, field

from werewolf.host import Host
from werewolf.player import Player
from werewolf.registry import Registry
from werewolf.settings import WerewolfSettings


@dataclass
class Deps:
    settings: WerewolfSettings
    registry: Registry
    player: Player
    host: Host = field(default_factory=Host)

