"""喂给 Werewolf Player / Actor 的视图对象。

这是一个 opaque 视图对象：core.sr.loop 不读字段，OpenCodeActor 只读 `.prompt`；
WerewolfRules.random_action 读 `.phase` / `.candidates` 等字段生成合法 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.threads import ThreadMessage
from werewolf.rules import Role
from werewolf.state import SeerCheck, VoteRecord


@dataclass(frozen=True)
class PublicDeath:
    """玩家有权看到的死亡账本条目：死者、翻牌与发生天数。

    内部 ``DeathRecord`` 还带有狼刀/毒杀等裁判结算信息；它不能越过 view
    边界，否则普通席位会从 prompt 反推出夜间行动。
    """

    seat_no: int
    role: Role
    day: int


@dataclass
class PlayerView:
    """某席位在当前行动点能看到的全部信息（私密信息隔离的唯一闸口）。"""

    seat_no: int
    role: Role
    players: int
    day: int
    phase: str
    alive_nos: list[int] = field(default_factory=list)
    dead_nos: list[int] = field(default_factory=list)
    wolf_allies: list[int] = field(default_factory=list)  # 仅狼人非空：发牌时的同伴名单（不随死亡变化）
    seer_checks: list[SeerCheck] = field(default_factory=list)  # 仅预言家非空
    witch_has_antidote: bool = True
    witch_has_poison: bool = True
    night_target: int | None = None  # 仅女巫行动时非空：今夜刀口
    candidates: list[int] = field(default_factory=list)  # 当前行动的合法目标座位（刀/验/毒/投/枪）
    deaths: list[PublicDeath] = field(default_factory=list)  # 公开账本：死者与翻牌，不含裁判死因
    votes_history: list[VoteRecord] = field(default_factory=list)  # 公开账本：放逐票型记录
    tone: str = ""
    strategy: str = ""
    messages: list[ThreadMessage] = field(default_factory=list)
    turn_prompt: str = ""
    prompt: str = ""
