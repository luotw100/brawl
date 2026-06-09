"""裁判节点（纯代码）：胜负判定、平票随机、无兜底快速失败。

check_node 不发事件、只看存活构成，可直接调用。tally_node / vote_node 会经 emit() 推
事件流——而 emit 在图运行上下文之外会抛 RuntimeError，故这两个测试把 nodes.emit 替成
空操作（事件内容由 console/recorder 那侧的契约负责，这里只验节点逻辑）。
"""

from __future__ import annotations

import random

import pytest

from undercover import nodes
from undercover.deps import Deps
from undercover.registry import Registry
from undercover.state import GameState, Role, Seat


def _seat(no: int, role: Role, *, alive: bool = True, persona_id: str = "fox", name: str = "x") -> Seat:
    return Seat(no=no, persona_id=persona_id, name=name, model=None, role=role, alive=alive)


# ---------------------------------------------------------------- check_node

async def test_check_civilians_win_when_all_undercover_dead() -> None:
    state = GameState(seats=[
        _seat(0, Role.CIVILIAN),
        _seat(1, Role.CIVILIAN),
        _seat(2, Role.UNDERCOVER, alive=False),
    ])
    out = await nodes.check_node(state, {})
    assert out == {"winner": "civilians", "finished": True}


async def test_check_undercover_win_when_parity_reached() -> None:
    """存活卧底数 ≥ 存活平民数 → 卧底胜（含 1v1 这种 v1 退化情形）。"""
    state = GameState(seats=[
        _seat(0, Role.UNDERCOVER),
        _seat(1, Role.CIVILIAN),
        _seat(2, Role.CIVILIAN, alive=False),
    ])
    out = await nodes.check_node(state, {})
    assert out == {"winner": "undercover", "finished": True}


async def test_check_continue_when_civilians_still_majority() -> None:
    state = GameState(seats=[
        _seat(0, Role.UNDERCOVER),
        _seat(1, Role.CIVILIAN),
        _seat(2, Role.CIVILIAN),
        _seat(3, Role.CIVILIAN),
    ])
    out = await nodes.check_node(state, {})
    assert out == {"finished": False}


async def test_multi_undercover_win_conditions_hold() -> None:
    """任意卧底数都按"构成"判，不看本轮被投者。覆盖三条分支。"""
    # 2 卧底全灭 → 平民胜
    s1 = GameState(seats=[
        _seat(0, Role.UNDERCOVER, alive=False),
        _seat(1, Role.UNDERCOVER, alive=False),
        _seat(2, Role.CIVILIAN),
    ])
    assert await nodes.check_node(s1, {}) == {"winner": "civilians", "finished": True}
    # 2 存活卧底 ≥ 1 存活平民 → 卧底胜
    s2 = GameState(seats=[
        _seat(0, Role.UNDERCOVER),
        _seat(1, Role.UNDERCOVER),
        _seat(2, Role.CIVILIAN),
        _seat(3, Role.CIVILIAN, alive=False),
    ])
    assert await nodes.check_node(s2, {}) == {"winner": "undercover", "finished": True}
    # 2 卧底 < 3 平民 → 继续
    s3 = GameState(seats=[
        _seat(0, Role.UNDERCOVER),
        _seat(1, Role.UNDERCOVER),
        _seat(2, Role.CIVILIAN),
        _seat(3, Role.CIVILIAN),
        _seat(4, Role.CIVILIAN),
    ])
    assert await nodes.check_node(s3, {}) == {"finished": False}


# ---------------------------------------------------------------- tally_node

async def test_tally_eliminates_top_voted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nodes, "emit", lambda *a, **k: None)
    state = GameState(
        round=1,
        seats=[_seat(0, Role.CIVILIAN), _seat(1, Role.UNDERCOVER), _seat(2, Role.CIVILIAN)],
        votes={0: 1, 2: 1, 1: 0},  # 1 号得 2 票，最高
    )
    deps = Deps(settings=None, registry=None, player=None, rng=random.Random(0))  # tally 只用 rng
    out = await nodes.tally_node(state, {"configurable": {"deps": deps}})
    assert out["last_eliminated"] == 1
    assert next(s for s in out["seats"] if s.no == 1).alive is False


async def test_tally_breaks_tie_within_tied_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """平票时裁判在并列最高者里随机定一人——结果必在并列集合内、且确定（seeded）。"""
    monkeypatch.setattr(nodes, "emit", lambda *a, **k: None)
    state = GameState(
        round=1,
        seats=[_seat(0, Role.CIVILIAN), _seat(1, Role.UNDERCOVER)],
        votes={0: 1, 1: 0},  # 0、1 各 1 票，平票
    )
    deps = Deps(settings=None, registry=None, player=None, rng=random.Random(0))
    out = await nodes.tally_node(state, {"configurable": {"deps": deps}})
    assert out["last_eliminated"] in {0, 1}


# ---------------------------------------------------------------- vote_node 无兜底

class _FixedVoter:
    """总是投同一个座位号——用来制造合法/非法两种情形。"""

    def __init__(self, target: int) -> None:
        self._target = target

    async def describe(self, view):  # noqa: ANN001 - 协议要求但本测试不调用
        raise AssertionError("describe 不应被 vote_node 调用")

    async def vote(self, view):  # noqa: ANN001
        from undercover.state import Vote

        return Vote(target_no=self._target, reason="r", monologue="m")


async def test_vote_illegal_target_raises(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """投了不存在的座位 → 立刻抛错中断本局（无兜底，不静默替换）。"""
    monkeypatch.setattr(nodes, "emit", lambda *a, **k: None)
    state = GameState(round=1, seats=[
        Seat(no=0, persona_id="fox", name="A", model=None, role=Role.CIVILIAN),
        Seat(no=1, persona_id="janitor", name="B", model=None, role=Role.UNDERCOVER),
    ])
    deps = Deps(settings=None, registry=registry, player=_FixedVoter(99), rng=random.Random(0))
    with pytest.raises(ValueError):
        await nodes.vote_node(state, {"configurable": {"deps": deps}})


async def test_vote_self_target_raises(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """投自己也是非法目标 → 同样抛错。"""
    monkeypatch.setattr(nodes, "emit", lambda *a, **k: None)
    state = GameState(round=1, seats=[
        Seat(no=0, persona_id="fox", name="A", model=None, role=Role.CIVILIAN),
        Seat(no=1, persona_id="janitor", name="B", model=None, role=Role.UNDERCOVER),
    ])
    deps = Deps(settings=None, registry=registry, player=_FixedVoter(0), rng=random.Random(0))  # 0 号投 0 号
    with pytest.raises(ValueError):
        await nodes.vote_node(state, {"configurable": {"deps": deps}})
