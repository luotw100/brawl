"""探针后端：对"每个席位在每次决策时**实际看到了什么**"做全轨迹断言。

schema 测试（test_avalon_flow）保证公开通道**装不下**秘密字段；本文件反过来保证
喂给玩家的视图**恰好正确**——既不多（同时行动不泄漏、夜晚情报不越权、私密查验结果
只给持有者）也不少（提问/线程就位）。做法：用一个包装离线后端（--player random，即注入
FakeBackend 的 LLMPlayer）的探针记录每一次 PlayerView，赛后逐条对照"应得信息"的独立预言
（rules.night_intel + 线程纪律）。

整局流程的状态空间组合爆炸、无法穷举，这是对"实际发生的每一步"的 100% 检查：
固定种子下确定可复现，多种子聚合覆盖五种决策阶段与三种终局。
"""

from __future__ import annotations

import random
from collections import Counter

from avalon import rules
from avalon.deps import Deps
from avalon.graph import build_graph
from avalon.player import Player, PlayerView, build_player
from avalon.recorder import open_recorder
from avalon.registry import load_registry
from avalon.run import _build_seats
from avalon.settings import AvalonSettings
from avalon.state import GameState, Side
from avalon.validation import GameSetup, resolve_and_validate


class ProbePlayer:
    """记录每一次喂进来的 (阶段, 视图)，决策本身委托给内层离线 Player（FakeBackend）。"""

    def __init__(self, inner: Player) -> None:
        self._inner = inner
        self.seen: list[tuple[str, PlayerView]] = []

    async def propose(self, view: PlayerView):
        self.seen.append(("propose", view))
        return await self._inner.propose(view)

    async def discuss(self, view: PlayerView):
        self.seen.append(("discuss", view))
        return await self._inner.discuss(view)

    async def vote_team(self, view: PlayerView):
        self.seen.append(("team_vote", view))
        return await self._inner.vote_team(view)

    async def play_quest(self, view: PlayerView):
        self.seen.append(("quest", view))
        return await self._inner.play_quest(view)

    async def assassinate(self, view: PlayerView):
        self.seen.append(("assassinate", view))
        return await self._inner.assassinate(view)

    async def lady_choose(self, view: PlayerView):
        self.seen.append(("lady_choose", view))
        return await self._inner.lady_choose(view)

    async def lady_announce(self, view: PlayerView):
        self.seen.append(("lady_announce", view))
        return await self._inner.lady_announce(view)

    async def banter(self, view: PlayerView):
        self.seen.append(("banter", view))
        return await self._inner.banter(view)


async def _run_probed(settings: AvalonSettings) -> tuple[GameSetup, ProbePlayer, GameState]:
    """绕过 build_player 工厂注入探针，其余编排与 run.run_game 一致。"""
    registry = load_registry()
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    inner = build_player(settings.player, rng, recorder=open_recorder(None), extractor=registry.models[settings.extractor])
    probe = ProbePlayer(inner)
    deps = Deps(settings=settings, registry=registry, player=probe)
    config = {"configurable": {"deps": deps}, "recursion_limit": 200}
    final: GameState | None = None
    initial = GameState(
        seats=_build_seats(registry, setup),
        leader=setup.first_leader,
        lady_holder=setup.lady_holder,
        lady_used=[setup.lady_holder] if setup.lady_holder is not None else [],
    )
    async for mode, chunk in build_graph().astream(initial, config=config, stream_mode=["custom", "values"]):
        if mode == "values":
            final = GameState(**chunk) if not isinstance(chunk, GameState) else chunk
    assert final is not None and final.finished
    return setup, probe, final


def _assert_view_exact(phase: str, view: PlayerView, setup: GameSetup) -> None:
    """单条视图的"应得信息"预言：不多也不少。座位号 1 起：roles[no-1] 即 no 号的角色。"""
    players = len(setup.roles)
    assert 1 <= view.seat_no <= players and view.players == players
    # 自己的角色/夜晚情报：与边界摇定的牌严格一致（刺杀阶段例外：红方终局互认）
    assert view.role is setup.roles[view.seat_no - 1]
    if phase == "assassinate":
        assert view.intel == [no for no in range(1, players + 1) if setup.roles[no - 1].side is Side.EVIL and no != view.seat_no]
    else:
        assert view.intel == rules.night_intel(setup.roles, view.seat_no)
    # 阵容是公开多重集
    assert Counter(view.lineup) == Counter(setup.roles)
    # 出牌阶段必须真在出发的队伍里
    if phase == "quest":
        assert view.seat_no in view.team
    # 湖中仙女的私密查验结果只在"当众宣布"那一刻、只给持有者本人；其余任何视图必为空
    if phase == "lady_announce":
        assert view.lady_target is not None
        assert view.lady_seen == setup.roles[view.lady_target - 1].side.value  # 真实阵营，不掺假
        assert view.lady_target not in view.lady_used  # 查验对象必须从未持有过令牌
    else:
        assert view.lady_target is None and view.lady_seen is None
    # 线程纪律：本轮提问非空；线程消息 human/assistant 严格交替、成对收尾（append-only 的形状）
    assert view.prompt
    assert len(view.messages) % 2 == 0
    assert all(m.role == ("human" if i % 2 == 0 else "assistant") for i, m in enumerate(view.messages))
    # 同时行动纪律：表决提问的事件块里绝不出现【开票】行——本轮的票还没收齐自然没有，
    # 上一轮的开票则在每人随后的发言/出牌回合就已 flush 进历史（人人每轮都行动）。
    if phase == "team_vote":
        assert "【开票】" not in view.prompt.split("当前局面：")[0]


async def test_every_view_carries_exactly_the_right_information() -> None:
    """多种子 × 两种桌型（5 人基础局 / 10 人全角色+湖中仙女局），逐视图全量断言；
    聚合后八种决策阶段必须全部出现过（含刺杀与湖中仙女），信息纪律全程零违例。"""
    phases_seen: set[str] = set()
    for seed in range(6):
        for settings in (
            AvalonSettings(player="random", players=5, seed=seed),
            AvalonSettings(player="random", players=10, percival=True, morgana=True, mordred=True, oberon=True, lady=True, seed=seed),
        ):
            setup, probe, _ = await _run_probed(settings)
            assert probe.seen, "一局至少应发生若干决策"
            for phase, view in probe.seen:
                _assert_view_exact(phase, view, setup)
            phases_seen |= {phase for phase, _ in probe.seen}
    assert phases_seen == {"propose", "discuss", "team_vote", "quest", "assassinate", "lady_choose", "lady_announce", "banter"}
