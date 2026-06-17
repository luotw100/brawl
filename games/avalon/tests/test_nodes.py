"""裁判节点（纯代码）：表决多数判定、否决计数与轮转、任务破坏票门槛、刺杀命中、无兜底快速失败。

check_node 与两个路由函数不发事件、只看任务轨，可直接调用。其余节点会经 emit() 推
事件流——而 emit 在图运行上下文之外会抛 RuntimeError，故这些测试把 nodes.emit 替成
空操作（事件内容由 console/recorder 那侧的契约负责，这里只验节点逻辑）。

座位号一律 1 起。桩后端按 Player 协议返回 (结构化结果, 自由文本原文) 二元组。
"""

from __future__ import annotations

from itertools import product

import pytest

from avalon import host as host_module
from avalon import nodes
from avalon.deps import Deps
from avalon.registry import Registry
from avalon.settings import AvalonSettings
from avalon.state import (
    Assassination,
    GameState,
    LadyChoice,
    Proposal,
    ProposalRecord,
    QuestCard,
    Role,
    Seat,
    Statement,
    TeamVote,
)


def _seat(no: int, role: Role = Role.SERVANT) -> Seat:
    return Seat(no=no, persona_id="fox", strategy_id="prudent", role=role)


def _seats(*roles: Role) -> list[Seat]:
    return [_seat(no, role) for no, role in enumerate(roles, start=1)]


def _played(quest: int, result: str) -> ProposalRecord:
    """凑任务轨用的已上场提案记录（团队/表决内容与被测逻辑无关）。"""
    return ProposalRecord(quest=quest, attempt=1, leader_no=1, team=[1, 2], votes={}, approved=True, fails=0 if result == "success" else 1, result=result)


def _mute_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    """图运行上下文外 get_stream_writer() 会抛——把节点与主持人两处 emit 都替成空操作。"""
    monkeypatch.setattr(nodes, "emit", lambda *a, **k: None)
    monkeypatch.setattr(host_module, "emit", lambda *a, **k: None)


def _deps(registry: Registry, player) -> dict:
    return {"configurable": {"deps": Deps(settings=AvalonSettings(player="random", players=5), registry=registry, player=player)}}


# ---------------------------------------------------------------- 桩后端（只实现被测节点会调到的方法）

class _FixedProposer:
    def __init__(self, team: list[int]) -> None:
        self._team = team

    async def propose(self, view):  # noqa: ANN001
        return Proposal(team=self._team, speech="s", monologue="m"), "raw"


class _ScriptedVoter:
    def __init__(self, votes: dict[int, bool]) -> None:
        self._votes = votes

    async def vote_team(self, view):  # noqa: ANN001
        return TeamVote(approve=self._votes[view.seat_no], monologue="m"), "raw"


class _ScriptedQuester:
    def __init__(self, cards: dict[int, bool]) -> None:
        self._cards = cards

    async def play_quest(self, view):  # noqa: ANN001
        return QuestCard(success=self._cards[view.seat_no], monologue="m"), "raw"


class _FixedAssassin:
    def __init__(self, target: int) -> None:
        self._target = target

    async def assassinate(self, view):  # noqa: ANN001
        return Assassination(target_no=self._target, speech="s", monologue="m"), "raw"


_FIVE = (Role.MERLIN, Role.SERVANT, Role.SERVANT, Role.ASSASSIN, Role.MINION)  # 1号梅林、4号刺客、5号爪牙


# ---------------------------------------------------------------- propose_node

async def test_propose_valid_team_resets_votes(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1)  # 5 人第 1 任务队伍 2 人
    out = await nodes.propose_node(state, _deps(registry, _FixedProposer([4, 2])))
    assert out["team"] == [2, 4]  # 入状态前排好序
    assert out["votes"] == {}
    # 队长线程落了一问一答；提案广播进了全员 inbox（含队长本人）
    assert [m.role for m in out["threads"][1].messages] == ["human", "assistant"]
    assert out["threads"][1].messages[1].content == "raw"
    for t in out["threads"].values():
        assert any("队长 1号 提议的队伍：2号、4号" in line for line in t.inbox)  # 队伍清单存档上桌（不念）
        assert any("下面从 2号 开始轮流发言" in line for line in t.inbox)  # 主持人只点名开启讨论轮（队长下家）
        assert any("1号：「s」" == line for line in t.inbox)  # 队长发言原文上桌


@pytest.mark.parametrize("team", [[1], [1, 2, 3], [1, 1], [0, 99]])
async def test_propose_illegal_team_raises(registry: Registry, monkeypatch: pytest.MonkeyPatch, team: list[int]) -> None:
    """人数不对/重复/不存在的座位（0 号不存在：座位 1 起）→ 立刻抛错中断本局（无兜底，不静默修正）。"""
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1)
    with pytest.raises(ValueError):
        await nodes.propose_node(state, _deps(registry, _FixedProposer(team)))


# ---------------------------------------------------------------- discuss_node

async def test_discuss_round_robin_from_leaders_next_seat(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """从队长下家起逐个表态（队长不重复发言）；后说者的 human 提问里能看到先说者的发言。"""
    _mute_emit(monkeypatch)
    order: list[int] = []

    class _Talker:
        async def discuss(self, view):  # noqa: ANN001
            order.append(view.seat_no)
            return Statement(speech=f"我是{view.seat_no}号", monologue="m"), "raw"

    state = GameState(seats=_seats(*_FIVE), leader=3, team=[1, 2])
    out = await nodes.discuss_node(state, _deps(registry, _Talker()))
    assert order == [4, 5, 1, 2]  # 3 号队长除外，从 4 号起环绕
    # 最后发言的 2 号，其 human 提问里有 4 号的表态（先说者可见）
    human = out["threads"][2].messages[0].content
    assert "我是4号" in human


# ---------------------------------------------------------------- team_vote_node

async def test_vote_majority_approves(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1, team=[1, 2])
    out = await nodes.team_vote_node(state, _deps(registry, _ScriptedVoter({1: True, 2: True, 3: True, 4: False, 5: False})))
    assert "history" not in out  # 通过：提案记录等任务出完再归档
    merged = state.model_copy(update=out)
    assert nodes.route_after_vote(merged) == "quest"


async def test_vote_tie_means_rejection_and_leader_rotates(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """6 人 3:3 平票=否决：记一次连续否决、队长轮转、提案以 approved=False 归档。"""
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE, Role.SERVANT), leader=6, team=[1, 2])
    out = await nodes.team_vote_node(
        state, _deps(registry, _ScriptedVoter({1: True, 2: True, 3: True, 4: False, 5: False, 6: False}))
    )
    assert out["rejections"] == 1
    assert out["leader"] == 1  # 6 号队长轮转回 1 号
    (record,) = out["history"]
    assert record.approved is False and record.result is None
    merged = state.model_copy(update=out)
    assert nodes.route_after_vote(merged) == "repropose"


async def test_vote_majority_threshold_exhaustive(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """5 人 2^5=32 种票型**全枚举**：通过当且仅当赞成 ≥3（严格过半，平票在偶数人局才可能）；
    否决恰好记一次连否并归档，通过则不归档（等任务出完）。"""
    _mute_emit(monkeypatch)
    for bits in product([False, True], repeat=5):
        state = GameState(seats=_seats(*_FIVE), leader=1, team=[1, 2])
        out = await nodes.team_vote_node(state, _deps(registry, _ScriptedVoter(dict(enumerate(bits, start=1)))))
        approved = sum(bits) >= 3
        assert ("history" not in out) is approved, f"票型 {bits} 归档时机判错"
        merged = state.model_copy(update=out)
        assert nodes.route_after_vote(merged) == ("quest" if approved else "repropose"), f"票型 {bits} 路由判错"
        if not approved:
            assert out["rejections"] == 1


async def test_vote_result_broadcast_to_every_thread(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """开票结果（含全部票型）在全部行动收齐后才广播进全员 inbox——表决期间谁的提问里都没有本轮他人的票。"""
    _mute_emit(monkeypatch)
    prompts_seen: dict[int, str] = {}

    class _SpyVoter:
        async def vote_team(self, view):  # noqa: ANN001
            prompts_seen[view.seat_no] = view.prompt
            return TeamVote(approve=True, monologue="m"), "raw"

    state = GameState(seats=_seats(*_FIVE), leader=1, team=[1, 2])
    out = await nodes.team_vote_node(state, _deps(registry, _SpyVoter()))
    for prompt in prompts_seen.values():
        assert "【开票】" not in prompt  # 同时行动：表决时看不到本轮任何票
    for t in out["threads"].values():
        assert any("【开票】" in line for line in t.inbox)  # 收齐后全员可见


async def test_fifth_rejection_hands_evil_the_win(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1, team=[1, 2], rejections=4)
    out = await nodes.team_vote_node(state, _deps(registry, _ScriptedVoter(dict.fromkeys(range(1, 6), False))))
    assert out["winner"] == "evil" and out["win_reason"] == "rejections" and out["finished"] is True
    merged = state.model_copy(update=out)
    assert nodes.route_after_vote(merged) == "end"


# ---------------------------------------------------------------- quest_node

async def test_quest_single_fail_card_fails_the_quest(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1, team=[1, 4], votes={1: True, 2: True, 3: True, 4: False, 5: False})
    out = await nodes.quest_node(state, _deps(registry, _ScriptedQuester({1: True, 4: False})))
    (record,) = out["history"]
    assert record.result == "fail" and record.fails == 1 and record.approved is True
    assert out["rejections"] == 0
    assert out["leader"] == 2


async def test_quest_all_success_succeeds(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1, team=[1, 2])
    out = await nodes.quest_node(state, _deps(registry, _ScriptedQuester({1: True, 2: True})))
    (record,) = out["history"]
    assert record.result == "success" and record.fails == 0


async def test_quest_four_at_seven_players_needs_two_fails(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """7 人局第 4 任务要 2 张破坏票：只有 1 张 → 任务照样成功。"""
    _mute_emit(monkeypatch)
    seats = _seats(*_FIVE, Role.SERVANT, Role.MINION)
    history = [_played(1, "success"), _played(2, "fail"), _played(3, "success")]
    state = GameState(seats=seats, leader=1, team=[1, 2, 3, 4], history=history)
    assert state.quest_no == 4
    out = await nodes.quest_node(state, _deps(registry, _ScriptedQuester({1: True, 2: True, 3: True, 4: False})))
    (record,) = out["history"]
    assert record.fails == 1 and record.result == "success"


async def test_quest_fail_threshold_exhaustive(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """破坏票门槛**全枚举**：全红队伍出 0..k 张破坏，门槛 1（5 人任务 1）与门槛 2
    （7 人任务 4）下逐一验证 result == (fails >= 门槛)。"""
    _mute_emit(monkeypatch)
    # 门槛 1：5 人局任务 1，3 人全红队（构成合法性由边界守，节点只看 side）
    seats5 = _seats(Role.ASSASSIN, Role.MINION, Role.MINION, Role.SERVANT, Role.SERVANT)
    for fails in range(4):
        cards = {no: no > fails for no in (1, 2, 3)}  # 前 fails 个出破坏
        state = GameState(seats=seats5, leader=1, team=[1, 2, 3])
        out = await nodes.quest_node(state, _deps(registry, _ScriptedQuester(cards)))
        (record,) = out["history"]
        assert record.fails == fails and record.result == ("fail" if fails >= 1 else "success")
    # 门槛 2：7 人局任务 4，4 人队含 3 红
    seats7 = _seats(Role.ASSASSIN, Role.MINION, Role.MINION, Role.SERVANT, Role.SERVANT, Role.SERVANT, Role.MERLIN)
    history = [_played(1, "success"), _played(2, "fail"), _played(3, "success")]
    for fails in range(4):
        cards = {no: no > fails for no in (1, 2, 3)} | {4: True}
        state = GameState(seats=seats7, leader=1, team=[1, 2, 3, 4], history=history)
        out = await nodes.quest_node(state, _deps(registry, _ScriptedQuester(cards)))
        (record,) = out["history"]
        assert record.fails == fails and record.result == ("fail" if fails >= 2 else "success")


async def test_good_player_playing_fail_raises(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """蓝方出破坏票是非法输出 → 立刻抛错中断本局（无兜底）。"""
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1, team=[2, 3])
    with pytest.raises(ValueError):
        await nodes.quest_node(state, _deps(registry, _ScriptedQuester({2: False, 3: True})))


# ---------------------------------------------------------------- check_node + 路由

async def test_three_failed_quests_finish_for_evil() -> None:
    state = GameState(seats=_seats(*_FIVE), history=[_played(1, "fail"), _played(2, "fail"), _played(3, "fail")])
    out = await nodes.check_node(state, {})
    assert out == {"winner": "evil", "win_reason": "quests", "finished": True}
    assert nodes.route_after_check(state.model_copy(update=out)) == "end"


async def test_three_succeeded_quests_route_to_assassination() -> None:
    state = GameState(seats=_seats(*_FIVE), history=[_played(1, "success"), _played(2, "success"), _played(3, "success")])
    assert await nodes.check_node(state, {}) == {}
    assert nodes.route_after_check(state) == "assassinate"


async def test_undecided_track_continues() -> None:
    state = GameState(seats=_seats(*_FIVE), history=[_played(1, "success"), _played(2, "fail")])
    assert await nodes.check_node(state, {}) == {}
    assert nodes.route_after_check(state) == "continue"


# ---------------------------------------------------------------- lady_node

class _ScriptedLady:
    """指定查验对象的桩：顺便捕获"当众宣布"时裁判私下递来的真实阵营。"""

    def __init__(self, target: int) -> None:
        self._target = target
        self.seen: str | None = None

    async def lady_choose(self, view):  # noqa: ANN001
        return LadyChoice(target_no=self._target, monologue="m"), "raw"

    async def lady_announce(self, view):  # noqa: ANN001
        self.seen = view.lady_seen
        return Statement(speech="s", monologue="m"), "raw"


async def test_lady_reveals_truth_and_passes_token(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """查验红方席位：持有者私下看到 evil；令牌传给被查者、其进入历任名单、宣布广播全员。"""
    _mute_emit(monkeypatch)
    lady = _ScriptedLady(target=4)  # 4 号是刺客
    state = GameState(seats=_seats(*_FIVE), leader=1, lady_holder=5, lady_used=[5])
    out = await nodes.lady_node(state, _deps(registry, lady))
    assert lady.seen == "evil"
    assert out["lady_holder"] == 4 and out["lady_used"] == [5, 4]
    # 持有者线程落了两问两答（查验 + 宣布）；宣布广播进全员 inbox；私密结果只进持有者的 human
    assert [m.role for m in out["threads"][5].messages] == ["human", "assistant", "human", "assistant"]
    assert "真实阵营" in out["threads"][5].messages[2].content
    for no, t in out["threads"].items():
        # 主持词对持有者在宣布前已 flush 进他的 human 提问，其余席位还躺在 inbox
        seen = "\n".join([m.content for m in t.messages if m.role == "human"] + list(t.inbox))
        assert "湖中仙女环节" in seen
        if no != 5:
            assert all("真实阵营" not in m.content for m in t.messages)


async def test_lady_cannot_target_previous_holder(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """查验持有过令牌的人（含自己）是非法目标 → 抛错（无兜底）。"""
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1, lady_holder=4, lady_used=[5, 4])
    with pytest.raises(ValueError):
        await nodes.lady_node(state, _deps(registry, _ScriptedLady(target=5)))


async def test_route_triggers_lady_only_after_quests_two_three_four() -> None:
    """查任务轨路由：启用湖中仙女时，恰在第 2/3/4 个任务结束后插入查验，其余照旧。"""
    def _state(played: list[str], lady_holder: int | None) -> GameState:
        history = [_played(q + 1, r) for q, r in enumerate(played)]
        return GameState(seats=_seats(*_FIVE), lady_holder=lady_holder, lady_used=[1] if lady_holder is not None else [], history=history)

    assert nodes.route_after_check(_state(["success"], lady_holder=1)) == "continue"  # 1 个任务后不触发
    assert nodes.route_after_check(_state(["success", "fail"], lady_holder=1)) == "lady"
    assert nodes.route_after_check(_state(["success", "fail", "fail"], lady_holder=1)) == "lady"
    assert nodes.route_after_check(_state(["success", "fail"], lady_holder=None)) == "continue"  # 未启用


# ---------------------------------------------------------------- assassinate_node

async def test_assassinating_merlin_wins_for_evil(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1)
    out = await nodes.assassinate_node(state, _deps(registry, _FixedAssassin(1)))  # 1 号是梅林
    assert out["winner"] == "evil" and out["win_reason"] == "assassination" and out["finished"] is True


async def test_missing_merlin_wins_for_good(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1)
    out = await nodes.assassinate_node(state, _deps(registry, _FixedAssassin(2)))  # 2 号是忠臣
    assert out["winner"] == "good" and out["win_reason"] == "assassination"


async def test_assassinating_evil_seat_raises(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    """刺向红方同伙（含自己）是非法目标 → 抛错（无兜底）。"""
    _mute_emit(monkeypatch)
    state = GameState(seats=_seats(*_FIVE), leader=1)
    with pytest.raises(ValueError):
        await nodes.assassinate_node(state, _deps(registry, _FixedAssassin(5)))  # 5 号是爪牙
