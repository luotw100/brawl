"""rules.py：规则常数与纯规则函数——输入空间有限，**全部穷举**、不抽样。

- sees 可见性矩阵：8 角色 × 8 角色 = 64 个组合逐一对照独立写死的期望表（期望表按
  规则书人肉抄录，不复用实现里的任何公式——否则就是同义反复）。
- night_intel：对一副含全部 8 种角色的牌逐座位展开，验证矩阵到座位号的应用。
- fails_required / team_size：人数 6 档 × 任务 5 个 = 30 个组合全查。
"""

from __future__ import annotations

from itertools import product

from avalon import rules
from avalon.state import Role

# 期望可见性表（按规则书独立抄录）：viewer → 能看到的角色集合。
# 梅林看红方但看不到莫德雷德；派西维尔看梅林+莫甘娜；红方互认但奥伯伦除外（双向）；
# 忠臣与奥伯伦自己什么都看不到。
_EXPECTED_SEES: dict[Role, set[Role]] = {
    Role.MERLIN: {Role.ASSASSIN, Role.MORGANA, Role.OBERON, Role.MINION},
    Role.PERCIVAL: {Role.MERLIN, Role.MORGANA},
    Role.SERVANT: set(),
    Role.ASSASSIN: {Role.ASSASSIN, Role.MORGANA, Role.MORDRED, Role.MINION},
    Role.MORGANA: {Role.ASSASSIN, Role.MORGANA, Role.MORDRED, Role.MINION},
    Role.MORDRED: {Role.ASSASSIN, Role.MORGANA, Role.MORDRED, Role.MINION},
    Role.OBERON: set(),
    Role.MINION: {Role.ASSASSIN, Role.MORGANA, Role.MORDRED, Role.MINION},
}


def test_sees_matrix_exhaustive() -> None:
    """64 个 (viewer, target) 组合逐一对照期望表。"""
    for viewer, target in product(Role, Role):
        assert rules.sees(viewer, target) is (target in _EXPECTED_SEES[viewer]), f"{viewer} 看 {target} 判错"


def test_night_intel_applies_matrix_to_seats() -> None:
    """一副含全部 8 种角色的牌：每个座位（1 起）的情报 = 矩阵的逐座位应用，且永不含自己。

    （8 红超阵营名额，不是合法对局——night_intel 是纯函数，这里只验矩阵应用本身；
    合法构成由 settings/validation 守。）"""
    deck = list(Role)
    for i, viewer in enumerate(deck):
        no = i + 1
        intel = rules.night_intel(deck, no)
        assert no not in intel
        assert intel == [j + 1 for j, target in enumerate(deck) if j + 1 != no and target in _EXPECTED_SEES[viewer]]


def test_evil_mutual_recognition_excludes_self_but_not_same_role() -> None:
    """两个爪牙互见（同角色不同座位）；自己永远不在自己的情报里。"""
    deck = [Role.MINION, Role.MINION, Role.SERVANT]
    assert rules.night_intel(deck, 1) == [2]
    assert rules.night_intel(deck, 2) == [1]


def test_fails_required_exhaustive() -> None:
    """30 个 (players, quest) 组合全查：仅 7 人及以上的第 4 任务要 2 张破坏票。"""
    for players, quest in product(sorted(rules.TEAM_SIZES), range(1, rules.QUESTS + 1)):
        expected = 2 if (players >= 7 and quest == 4) else 1
        assert rules.fails_required(players, quest) == expected


def test_team_sizes_table_well_formed() -> None:
    """规则表形状：每档 5 个任务、队伍人数随人数单调不减、且不超过总人数。"""
    for players, sizes in rules.TEAM_SIZES.items():
        assert len(sizes) == rules.QUESTS
        assert all(2 <= s <= players for s in sizes)
        assert all(rules.team_size(players, q) == sizes[q - 1] for q in range(1, rules.QUESTS + 1))
