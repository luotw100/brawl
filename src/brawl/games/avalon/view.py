"""喂给 Avalon Player / Actor 的视图对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from brawl.games.avalon.state import ProposalRecord, Role
from brawl.runtime.threads import ThreadMessage


@dataclass
class PlayerView:
    """某席位在当前行动点能看到的全部信息。

    这是一个 opaque 视图对象：brawl.runtime.loop 不读字段，OpenCodeActor 只读 `.prompt`；
    OpenCodeBackend 再读取这里声明的传输协议。RandomActor 配 rules.random_action
    读 `.phase` 等字段生成合法 JSON。

    `.schema` 只供旧 v1 replay 的 StructuredOutput 协议。v2+ 用角色 `.agent`、一次性
    `.seed_prompt`、当前唯一 `.action_tool` 与 `.speech_required`。该布尔值只表示是否
    要求工具外 assistant 文本；v15 的玩家发言来自当前工具的 `speech` 参数。
    """

    seat_no: int
    role: Role
    intel: list[int]
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
    current_votes: dict[int, bool] = field(default_factory=dict)
    lady_used: list[int] = field(default_factory=list)
    lady_checks: dict[int, str] = field(default_factory=dict)
    lady_target: int | None = None
    lady_seen: str | None = None
    tone: str = ""
    strategy: str = ""
    history: list[ProposalRecord] = field(default_factory=list)
    inbox: list[str] = field(default_factory=list)
    messages: list[ThreadMessage] = field(default_factory=list)
    turn_prompt: str = ""
    prompt: str = ""
    phase: str = ""
    evil_team: list[int] = field(default_factory=list)
    schema: dict[str, Any] | None = None
    agent: str = ""
    seed_prompt: str = ""
    action_tool: str | None = None
    speech_required: bool = False
    agent_protocol: int = 1
    public_position: str | None = None

    def others(self) -> list[int]:
        return [no for no in range(1, self.players + 1) if no != self.seat_no]

    def good_suspects(self) -> list[int]:
        """刺客候选：终局互认后，排除已经明确告知的完整红方名单。"""
        known_evil = self.evil_team or [self.seat_no, *self.intel]
        return [no for no in self.others() if no not in known_evil]

    def lady_candidates(self) -> list[int]:
        """湖中仙女可查验对象：从未持有过令牌的人。"""
        return [no for no in range(1, self.players + 1) if no not in self.lady_used]
