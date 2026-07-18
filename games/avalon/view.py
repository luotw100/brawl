"""喂给 Avalon Player / Actor 的视图对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from avalon.state import ProposalRecord, Role
from core.threads import ThreadMessage


@dataclass
class PlayerView:
    """某席位在当前行动点能看到的全部信息。

    这是一个 opaque 视图对象：core.sr.loop 不读字段，OpenCodeActor 只读 `.prompt` 和
    `.seat_no`；AvalonRandomActor 读 `.phase` 等字段生成合法 JSON。
    """

    seat_no: int
    role: Role
    intel: list[int]
    model: Any = None
    lineup: list[Role] = field(default_factory=list)
    players: int = 0
    leader_no: int = 0
    quest_no: int = 1
    attempt: int = 1
    team_size: int = 0
    fails_required: int = 1
    succeeded: int = 0
    failed: int = 0
    team: list[int] = field(default_factory=list)
    lady_used: list[int] = field(default_factory=list)
    lady_target: int | None = None
    lady_seen: str | None = None
    tone: str = ""
    strategy: str = ""
    history: list[ProposalRecord] = field(default_factory=list)
    messages: list[ThreadMessage] = field(default_factory=list)
    turn_prompt: str = ""
    prompt: str = ""
    phase: str = ""

    def others(self) -> list[int]:
        return [no for no in range(1, self.players + 1) if no != self.seat_no]

    def good_suspects(self) -> list[int]:
        """红方视角下可能是梅林的候选（除自己和已知同伙外）。"""
        return [no for no in self.others() if no not in self.intel]

    def lady_candidates(self) -> list[int]:
        """湖中仙女可查验对象：从未持有过令牌的人。"""
        return [no for no in range(1, self.players + 1) if no not in self.lady_used]
