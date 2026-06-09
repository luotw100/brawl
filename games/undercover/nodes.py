"""裁判节点（纯代码）。AI 只决定"说什么"，何时说、怎么计票、判胜负全在这里。

无兜底：AI 调用失败/超时（ModelSpec.timeout）/输出非法一律抛错中断本局，让问题立刻
暴露，绝不静默替换成占位或随机内容。唯一的"随机"是平票随机淘汰（tally_node），那是
v1 约定的游戏规则、依赖运行期票数，故只能留在节点；其余"开局随机"（身份/词）已前置到
边界 validation.resolve_and_validate 摇定，节点拿到的是确定值。无人值守的"崩了重开下一
局"应由外层看护进程负责。
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from undercover.deps import Deps
from undercover.events import emit
from undercover.player import PlayerView
from undercover.registry import ModelSpec
from undercover.state import GameState, Role, Seat, Utterance


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


def _model_spec(deps: Deps, seat: Seat) -> ModelSpec | None:
    if seat.model is None:
        return None
    return deps.registry.models.get(seat.model)


def _view(deps: Deps, state: GameState, seat: Seat, transcript: list[Utterance]) -> PlayerView:
    alive = state.alive_seats()
    return PlayerView(
        seat_no=seat.no,
        name=seat.name,
        word=seat.word,
        persona_system=deps.registry.personas[seat.persona_id].system,
        strategy_system=deps.registry.strategies[seat.strategy_id].system,
        model=_model_spec(deps, seat),
        roster=[(s.no, s.name) for s in alive],
        transcript=transcript,
        round=state.round,
    )


# ---------------------------------------------------------------- 阶段节点

async def pregame_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """开局公布（纯发布，与 postgame 对称）：把已定好的对局构成与全部底牌亮给观众
    （god-view）。身份/词在边界（validation.resolve_and_validate）就已摇定并填进初始
    Seat，这里不决定任何东西、不碰 rng，只把它发上事件流。"""
    deps = _deps(config)
    s = deps.settings
    # 词从已发好的座位反推：blank=0 保证场上必有 ≥1 卧底、≥2 平民
    civ_word = next(x.word for x in state.seats if x.role is Role.CIVILIAN)
    undercover_word = next(x.word for x in state.seats if x.role is Role.UNDERCOVER)

    emit("game_start", players=s.players, undercover=s.undercover, blank=s.blank)
    emit(
        "deal",
        civ_word=civ_word,
        undercover_word=undercover_word,
        seats=[{"no": x.no, "name": x.name, "role": x.role.value, "word": x.word, "model": x.model, "strategy": x.strategy_id} for x in state.seats],
    )
    return {}


async def describe_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """描述轮：round-robin，每位存活者一句描述 + 内心独白。"""
    deps = _deps(config)
    round_no = state.round + 1
    emit("round_start", round=round_no)

    working = list(state.transcript)  # 含本轮已发言者，供后说的人看到
    new: list[Utterance] = []
    for seat in state.alive_seats():
        view = _view(deps, state.model_copy(update={"round": round_no}), seat, working)
        result = await deps.player.describe(view)  # 失败/超时直接抛错，不兜底
        utt = Utterance(round=round_no, seat_no=seat.no, name=seat.name, kind="describe", text=result.description)
        working.append(utt)
        new.append(utt)
        emit("describe", round=round_no, seat_no=seat.no, name=seat.name, text=result.description, monologue=result.monologue)

    return {"round": round_no, "transcript": new}


async def vote_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """投票轮：round-robin，每位存活者投一人 + 一句理由。"""
    deps = _deps(config)
    alive_nos = {s.no for s in state.alive_seats()}
    working = list(state.transcript)
    new: list[Utterance] = []
    votes: dict[int, int] = {}

    for seat in state.alive_seats():
        view = _view(deps, state, seat, working)
        result = await deps.player.vote(view)  # 失败/超时直接抛错，不兜底
        if result.target_no not in alive_nos or result.target_no == seat.no:
            raise ValueError(
                f"{seat.no}号{seat.name} 投出非法目标 {result.target_no}（可投：{sorted(alive_nos - {seat.no})}）"
            )
        votes[seat.no] = result.target_no
        target_name = next(s.name for s in state.seats if s.no == result.target_no)
        utt = Utterance(round=state.round, seat_no=seat.no, name=seat.name, kind="vote", text=result.reason, target_no=result.target_no)
        working.append(utt)
        new.append(utt)
        emit("vote", round=state.round, seat_no=seat.no, name=seat.name, target_no=result.target_no, target_name=target_name, reason=result.reason, monologue=result.monologue)

    return {"transcript": new, "votes": votes}


async def tally_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """计票出局：统计票数，平票随机出一人（裁判定），公布并揭晓出局者身份。"""
    deps = _deps(config)
    counts: dict[int, int] = {}
    for target in state.votes.values():
        counts[target] = counts.get(target, 0) + 1

    top = max(counts.values())
    tied = [no for no, c in counts.items() if c == top]
    tie = len(tied) > 1
    eliminated_no = deps.rng.choice(tied) if tie else tied[0]

    seats = [s.model_copy(update={"alive": False}) if s.no == eliminated_no else s for s in state.seats]
    elim = next(s for s in seats if s.no == eliminated_no)

    emit("tally", round=state.round, counts=counts, tie=tie, eliminated_no=eliminated_no, eliminated_name=elim.name, eliminated_role=elim.role.value)
    return {"seats": seats, "last_eliminated": eliminated_no}


async def check_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """判胜负（按存活构成，支持任意卧底数）：

    - 所有卧底出局 → 平民胜；
    - 存活卧底数 ≥ 存活平民数（卧底已无法被多数票投出/已控场）→ 卧底胜；
    - 否则继续。

    对"1 卧底"会精确退化成 v1 的"场上仅剩 2 人卧底胜"。仅看构成、不看本轮被投者，
    故平票随机淘汰等任何出局方式都被一致处理。
    """
    alive = state.alive_seats()
    alive_undercover = [s for s in alive if s.role is Role.UNDERCOVER]
    alive_civilian = [s for s in alive if s.role is Role.CIVILIAN]
    if not alive_undercover:
        return {"winner": "civilians", "finished": True}
    if len(alive_undercover) >= len(alive_civilian):
        return {"winner": "undercover", "finished": True}
    return {"finished": False}


async def postgame_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """赛后结算：揭晓全部身份与胜负。"""
    emit(
        "game_end",
        winner=state.winner,
        rounds=state.round,
        seats=[{"no": s.no, "name": s.name, "role": s.role.value, "word": s.word, "alive": s.alive, "model": s.model} for s in state.seats],
    )
    return {}


def route_after_check(state: GameState) -> str:
    return "end" if state.finished else "continue"
