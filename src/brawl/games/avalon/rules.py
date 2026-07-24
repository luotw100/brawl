"""阿瓦隆的规则常数与纯规则函数（标准桌游规则）。

settings 的形状校验（players 是否在 5..10）、提案的队伍人数、任务的破坏票门槛、
夜晚情报的可见性，全查这里——规则只此一份，节点与校验不各自硬编码。
这里全是纯函数/常数表，输入空间有限，测试可以**穷举**（见 tests/test_rules.py）。
"""

from __future__ import annotations

from dataclasses import dataclass

from brawl.games.avalon.state import Role, Side

QUESTS = 5  # 一局共 5 个任务
QUESTS_TO_WIN = 3  # 任一方拿下 3 个任务即定任务轨胜负
MAX_REJECTIONS = 5  # 同一任务连续 5 次否决组队 → 红方直接获胜

# 人数 → 每个任务的队伍人数（标准规则表）
TEAM_SIZES: dict[int, tuple[int, int, int, int, int]] = {
    5: (2, 3, 2, 3, 3),
    6: (2, 3, 4, 3, 4),
    7: (2, 3, 3, 4, 4),
    8: (3, 4, 4, 5, 5),
    9: (3, 4, 4, 5, 5),
    10: (3, 4, 4, 5, 5),
}

# 人数 → 红方（坏人）数量
EVIL_COUNT: dict[int, int] = {5: 2, 6: 2, 7: 3, 8: 3, 9: 3, 10: 4}

# 湖中仙女（可选模块）：第 2、3、4 个任务结束后各触发一次查验
LADY_QUESTS = (2, 3, 4)


@dataclass(frozen=True)
class BoardSpec:
    """一个板子：固定人数与角色构成 + 湖中仙女开关。

    角色构成在此定死（线上只打 10 人全角色带湖仙局）；人数相关的规则表
    （TEAM_SIZES/EVIL_COUNT/fails_required）仍按 players 查，板子只决定发哪副牌。
    """

    id: str
    name: str
    roles: tuple[Role, ...]
    lady: bool

    @property
    def players(self) -> int:
        return len(self.roles)


MERLIN_ASSASSIN_PERCIVAL_MORGANA_MORDRED_OBERON_LADY_10 = BoardSpec(
    id="merlin-assassin-percival-morgana-mordred-oberon-lady-10",
    name="阿瓦隆10人全角色（带湖中仙女）",
    roles=(
        Role.MERLIN,
        Role.PERCIVAL,
        Role.SERVANT,
        Role.SERVANT,
        Role.SERVANT,
        Role.SERVANT,
        Role.ASSASSIN,
        Role.MORGANA,
        Role.MORDRED,
        Role.OBERON,
    ),
    lady=True,
)

BOARDS: dict[str, BoardSpec] = {
    MERLIN_ASSASSIN_PERCIVAL_MORGANA_MORDRED_OBERON_LADY_10.id: MERLIN_ASSASSIN_PERCIVAL_MORGANA_MORDRED_OBERON_LADY_10
}
DEFAULT_BOARD = MERLIN_ASSASSIN_PERCIVAL_MORGANA_MORDRED_OBERON_LADY_10.id


def board_by_id(board_id: str) -> BoardSpec:
    return BOARDS[board_id]


def team_size(players: int, quest_no: int) -> int:
    return TEAM_SIZES[players][quest_no - 1]


def fails_required(players: int, quest_no: int) -> int:
    """该任务需要几张破坏票才算失败：标准规则里仅 7 人及以上的第 4 任务要 2 张。"""
    return 2 if quest_no == 4 and players >= 7 else 1


def sees(viewer: Role, target: Role) -> bool:
    """夜晚可见性矩阵（标准规则）：viewer 在夜里能否看到 target 这张角色牌的座位。

    - 梅林看到全部红方，唯独看不到莫德雷德；
    - 派西维尔看到梅林与莫甘娜（两人难辨，由 prompt 措辞体现）；
    - 红方互认，唯独奥伯伦不参与互认（看不到人、也不被看到）；
    - 忠臣与奥伯伦自己什么都看不到。
    """
    if viewer is Role.MERLIN:
        return target.side is Side.EVIL and target is not Role.MORDRED
    if viewer is Role.PERCIVAL:
        return target in (Role.MERLIN, Role.MORGANA)
    if viewer.side is Side.EVIL and viewer is not Role.OBERON:
        return target.side is Side.EVIL and target is not Role.OBERON
    return False


def night_intel(roles: list[Role], no: int) -> list[int]:
    """no 号座位（1..N）开局拿到的夜晚情报（座位号列表，不含自己）——sees 矩阵对一副牌
    的逐座位应用。roles 按发牌顺序排列：roles[i] 即 i+1 号座位的角色。"""
    return [i + 1 for i, target in enumerate(roles) if i + 1 != no and sees(roles[no - 1], target)]
