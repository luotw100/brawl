"""裁判节点（纯代码）。AI 只决定"说什么/带谁/投什么"，何时行动、怎么计票、判胜负全在这里。

无兜底：AI 输出非法（队伍人数不对、蓝方出破坏票、刺杀红方同伙）一律抛错中断本局，
让问题立刻暴露，绝不静默替换成占位或随机内容。对局过程没有任何裁判侧随机——开局摇牌
（角色/首任队长）已前置到边界 validation.resolve_and_validate，表决平票按规则即否决。
无人值守的"崩了重开下一局"应由外层看护进程负责。

线程纪律：每席位一条 append-only 消息线程（state.threads）。公开记录分两类灌进全员 inbox：
主持人串场台词经 deps.host.announce（同时 emit host 事件，与线程一字不差同源、将来逐句 TTS），
玩家发言原文经 _broadcast（发言已在结构化事件里，主持人不复述）。轮到某席位行动时把它的
inbox flush 成一条 human 消息、模型的自由文本原文存为 assistant 消息——已发消息绝不回改
（prompt 缓存与"模型能看到自己之前的想法"都靠这条）。

同时行动纪律：组队表决与任务出牌是**同时**亮牌/扣牌——asyncio.gather 真并发调用（贴合
规则语义，settings.concurrency 信号量限流），开票/开牌结果在全部行动收齐后才广播；
表决前任何席位的 inbox 都不含他人本轮的票，任务暗牌永不公开、只公开张数。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig

from avalon import prompts, rules
from avalon.deps import Deps
from avalon.player import PlayerView
from avalon.registry import ModelSpec
from avalon.state import GameState, ProposalRecord, Role, Seat, Side
from core.events import decision_id, emit
from core.threads import SeatThread, copy_threads
from core.threads import broadcast as _broadcast
from core.threads import commit as _commit


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


def _model_spec(deps: Deps, seat: Seat) -> ModelSpec | None:
    if seat.model is None:
        return None
    spec = deps.registry.models.get(seat.model)
    if spec is None:
        raise ValueError(f"座位 {seat.no} 的模型 {seat.model!r} 未在 registry 中注册")
    return spec


def _threads_copy(state: GameState) -> dict[int, SeatThread]:
    return copy_threads(state.threads)


def _ask_view(
    deps: Deps, state: GameState, seat: Seat, threads: dict[int, SeatThread], phase: str,
    team: list[int] | None = None,
    with_situation: bool = True,
    lady_target: int | None = None,
    lady_seen: str | None = None,
    mimicry_guard: bool = True,
) -> PlayerView:
    """拼一个"带本轮提问"的视图：human 消息 = 该席位 inbox + 当前局面 + 该阶段 JSON 提问。"""
    players = len(state.seats)
    # 刺杀阶段任务轨已收官，quest_no 可能越过第 5 任务；队伍人数取末任务值仅为占位
    q = min(state.quest_no, rules.QUESTS)
    view = PlayerView(
        seat_no=seat.no,
        tone=deps.registry.personas[seat.persona_id].tone,
        strategy=deps.registry.strategies[seat.strategy_id].system,
        role=seat.role,
        intel=list(seat.intel),
        model=_model_spec(deps, seat),
        # 阵容是公开信息（角色多重集，按枚举序排定）；哪个座位是哪个角色才是秘密
        lineup=sorted((s.role for s in state.seats), key=tuple(Role).index),
        players=players,
        leader_no=state.leader,
        quest_no=state.quest_no,
        attempt=state.attempt,
        team_size=rules.team_size(players, q),
        fails_required=rules.fails_required(players, q),
        succeeded=state.quests_succeeded,
        failed=state.quests_failed,
        team=list(team) if team is not None else [],
        lady_used=list(state.lady_used),
        lady_target=lady_target,
        lady_seen=lady_seen,
        history=list(state.history),
        messages=list(threads[seat.no].messages),
    )
    ask = prompts.opencode_ask(phase, view)
    context = prompts.situation(view) if with_situation else ""
    view.prompt = prompts.human_message(list(threads[seat.no].inbox), context, ask, view.tone, mimicry_guard=mimicry_guard)
    return view


# ---------------------------------------------------------------- 阶段节点

async def pregame_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """开局公布（纯发布，与 postgame 对称）：把已定好的对局构成与全部底牌亮给观众
    （god-view）。角色/情报/首任队长在边界（validation→run._build_seats）就已摇定并
    填进初始状态，这里不决定任何东西，只把它发上事件流。"""
    deps = _deps(config)
    s = deps.settings
    seats_payload = [
        {"no": x.no, "persona": x.persona_id, "strategy": x.strategy_id, "role": x.role.value, "side": x.role.side.value, "intel": x.intel, "model": x.model}
        for x in state.seats
    ]
    threads = _threads_copy(state)
    first_team_size = rules.team_size(s.players, state.quest_no)
    # 开场节目单（事件调用顺序即 feed 顺序）：头像先就座（身份面朝下）→ 发牌逐张翻面亮身份，
    # 主持人开场口播盖在发牌动画下面，一句带出人数、仙女、队长与首队规模。
    emit("game_start", players=s.players, good=s.good, evil=s.evil, seats=seats_payload)
    emit(
        "deal",
        leader_no=state.leader,
        lady_holder=state.lady_holder,  # None = 本局未启用湖中仙女
        seats=seats_payload,
    )
    deps.host.announce(
        threads, "opening",
        prompts.line_host_opening(s.players, state.lady_holder, state.leader, first_team_size),
        quest=state.quest_no, attempt=state.attempt,
    )
    return {"threads": threads}


async def propose_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """组队：当前队长提一支人数符合规则表的队伍 + 组队宣言。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    leader = next(s for s in state.seats if s.no == state.leader)
    size = rules.team_size(len(state.seats), state.quest_no)

    view = _ask_view(deps, state, leader, threads, "propose")
    result, raw = await deps.player.propose(view)
    nos = {s.no for s in state.seats}
    if len(result.team) != size or len(set(result.team)) != size or not set(result.team) <= nos:
        raise ValueError(f"{leader.no}号 提出非法队伍 {result.team}（需 {size} 个互不相同的在场座位，可选：{sorted(nos)}）")
    team = sorted(result.team)

    _commit(threads, leader.no, view.prompt, raw)
    _broadcast(threads, prompts.line_player(leader.no, result.speech))
    # 队伍清单是线程里历史队伍的唯一结构化存档，但不是主持词：走广播进 inbox、不念出口。
    _broadcast(threads, prompts.line_team_record(leader.no, team))
    emit(
        "propose",
        quest=state.quest_no,
        attempt=state.attempt,
        call_id=decision_id(state.quest_no, state.attempt, "propose", leader.no),
        leader_no=leader.no,
        team=team,
        speech=result.speech,
        monologue=result.monologue,
    )
    return {"team": team, "votes": {}, "threads": threads}


async def discuss_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """讨论轮：从队长下家起 round-robin，每人对当前提案表一次态。后说的人能看到本轮先说者的发言。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    n = len(state.seats)
    for i in range(n - 1):  # 除队长外全员（队长的话已在组队宣言里）
        seat = state.seats[(state.leader + i) % n]
        view = _ask_view(deps, state, seat, threads, "discuss", team=state.team)
        result, raw = await deps.player.discuss(view)
        _commit(threads, seat.no, view.prompt, raw)
        _broadcast(threads, prompts.line_player(seat.no, result.speech))
        emit(
            "discuss",
            quest=state.quest_no,
            attempt=state.attempt,
            call_id=decision_id(state.quest_no, state.attempt, "discuss", seat.no),
            seat_no=seat.no,
            text=result.speech,
            monologue=result.monologue,
        )
    return {"threads": threads}


async def team_vote_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """组队表决：全员同时亮票（真并发），过半数赞成即出发（平票=否决）。被否决则记一次
    连续否决、队长轮转；同一任务第 5 次否决，红方直接获胜。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    sem = asyncio.Semaphore(deps.settings.concurrency)

    async def one(seat: Seat):
        view = _ask_view(deps, state, seat, threads, "team_vote", team=state.team)
        async with sem:
            result, raw = await deps.player.vote_team(view)
        return seat.no, view.prompt, result, raw

    results = await asyncio.gather(*(one(s) for s in state.seats))
    votes: dict[int, bool] = {}
    monologues: dict[int, str] = {}
    for no, prompt, result, raw in results:
        votes[no] = result.approve
        monologues[no] = result.monologue
        _commit(threads, no, prompt, raw)

    approved = sum(votes.values()) * 2 > len(state.seats)
    rejections = state.rejections if approved else state.rejections + 1
    _broadcast(threads, prompts.line_votes(votes))  # 票型明细是公开账本，进线程但不是主持词
    emit(
        "vote_result",
        quest=state.quest_no,
        attempt=state.attempt,
        votes=[
            {
                "seat_no": s.no,
                "approve": votes[s.no],
                "monologue": monologues[s.no],
                "call_id": decision_id(state.quest_no, state.attempt, "team_vote", s.no),
            }
            for s in state.seats
        ],
        approves=sum(votes.values()),
        approved=approved,
        rejections=rejections,
    )
    # 全员的票都亮完（vote_result 事件）后主持人才收口总结——回放层先逐个翻票、再播这句更自然。
    next_leader = state.leader % len(state.seats) + 1
    deps.host.announce(
        threads, "vote_result",
        prompts.line_host_vote_result(
            sum(votes.values()),
            len(votes),
            approved,
            rejections,
            None if approved or rejections >= rules.MAX_REJECTIONS else next_leader,
        ),
        quest=state.quest_no, attempt=state.attempt,
    )
    if approved:
        return {"votes": votes, "threads": threads}

    record = ProposalRecord(
        quest=state.quest_no, attempt=state.attempt, leader_no=state.leader,
        team=list(state.team), votes=votes, approved=False,
    )
    out: dict[str, Any] = {
        "votes": votes,
        "threads": threads,
        "history": [record],
        "rejections": rejections,
        "leader": next_leader,
    }
    if rejections >= rules.MAX_REJECTIONS:
        out |= {"winner": "evil", "win_reason": "rejections", "finished": True}
    return out


async def quest_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """执行任务：队员同时暗出成功/破坏（真并发；蓝方只能出成功，违者抛错），只公开破坏票
    张数。出完任务清零连续否决数、队长轮转，提案连同结果归档进公开 history。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    players = len(state.seats)
    need = rules.fails_required(players, state.quest_no)
    sem = asyncio.Semaphore(deps.settings.concurrency)

    async def one(seat: Seat):
        view = _ask_view(deps, state, seat, threads, "quest", team=state.team)
        async with sem:
            card, raw = await deps.player.play_quest(view)
        return seat, view.prompt, card, raw

    members = [next(s for s in state.seats if s.no == no) for no in state.team]
    results = await asyncio.gather(*(one(s) for s in members))
    cards: list[dict[str, Any]] = []
    fails = 0
    for seat, prompt, card, raw in results:
        if seat.role.side is Side.GOOD and not card.success:
            raise ValueError(f"{seat.no}号 是蓝方却出了破坏票——蓝方只能出成功")
        fails += 0 if card.success else 1
        cards.append({
            "seat_no": seat.no,
            "success": card.success,
            "monologue": card.monologue,
            "call_id": decision_id(state.quest_no, state.attempt, "quest", seat.no),
        })
        _commit(threads, seat.no, prompt, raw)

    result = "fail" if fails >= need else "success"
    succeeded = state.quests_succeeded + (1 if result == "success" else 0)
    failed = state.quests_failed + (1 if result == "fail" else 0)
    record = ProposalRecord(
        quest=state.quest_no, attempt=state.attempt, leader_no=state.leader,
        team=list(state.team), votes=dict(state.votes), approved=True, fails=fails, result=result,
    )
    emit(
        "quest_result",
        quest=state.quest_no,
        team=list(state.team),
        cards=cards,  # god-view：谁出的破坏票只进事件流，绝不进 AI 可见的任何通道
        fails=fails,
        fails_required=need,
        result=result,
        succeeded=succeeded,
        failed=failed,
    )
    # 暗牌全部开完（quest_result 事件）后主持人才公布结果——回放层先逐张翻牌、再播这句更自然。
    next_leader = state.leader % players + 1
    terminal = failed >= rules.QUESTS_TO_WIN or succeeded >= rules.QUESTS_TO_WIN
    lady_next = state.lady_holder is not None and state.quest_no in rules.LADY_QUESTS
    deps.host.announce(
        threads, "quest_result",
        prompts.line_host_quest_result(
            state.quest_no,
            fails,
            result,
            succeeded,
            failed,
            None if terminal or lady_next else next_leader,
        ),
        quest=state.quest_no, attempt=state.attempt,
    )
    return {"history": [record], "rejections": 0, "leader": next_leader, "threads": threads}


async def check_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """看任务轨：红方拿满 3 个失败任务即获胜终局；蓝方拿满 3 个成功任务**不**在此终局——
    还要过刺杀关（由 route_after_check 引向 assassinate）；否则继续下一任务。"""
    if state.quests_failed >= rules.QUESTS_TO_WIN:
        return {"winner": "evil", "win_reason": "quests", "finished": True}
    return {}


async def lady_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """湖中仙女：第 2/3/4 个任务结束后，令牌持有者查验一名从未持有过令牌者的真实阵营
    （只有持有者看到），随后公开发言（可如实可撒谎），令牌传给被查者。选谁、说什么是
    公开信息（广播进全员线程）；查到的真实阵营只进持有者自己的线程与 god-view 事件流。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    holder = next(s for s in state.seats if s.no == state.lady_holder)
    candidates = {s.no for s in state.seats} - set(state.lady_used)

    view = _ask_view(deps, state, holder, threads, "lady_choose")
    choice, raw = await deps.player.lady_choose(view)
    if choice.target_no not in candidates:
        raise ValueError(f"{holder.no}号 查验了非法对象 {choice.target_no}（只能查从未持有过令牌的人：{sorted(candidates)}）")
    _commit(threads, holder.no, view.prompt, raw)
    target = next(s for s in state.seats if s.no == choice.target_no)
    truth = target.role.side.value

    # 主持人先宣布查验对象与令牌去向（结构性事实），再由持有者自己当众宣布结果（玩家发言）。
    # 必须在拼 view2 之前广播：view2 flush 后 _commit 会清空 inbox，后插的行会丢。
    deps.host.announce(
        threads, "lady", prompts.line_host_lady(holder.no, target.no),
        quest=state.quest_no, attempt=state.attempt,
    )
    view2 = _ask_view(deps, state, holder, threads, "lady_announce", with_situation=False, lady_target=target.no, lady_seen=truth)
    announce, raw2 = await deps.player.lady_announce(view2)
    _commit(threads, holder.no, view2.prompt, raw2)
    _broadcast(threads, prompts.line_player(holder.no, announce.speech))
    emit(
        "lady",
        holder_no=holder.no,
        target_no=target.no,
        truth=truth,  # god-view：真实阵营只进事件流，其他 AI 只能听到 speech
        choose_call_id=decision_id(state.quest_no, state.attempt, "lady_choose", holder.no),
        announce_call_id=decision_id(state.quest_no, state.attempt, "lady_announce", holder.no),
        choose_monologue=choice.monologue,
        speech=announce.speech,
        monologue=announce.monologue,
    )
    return {"lady_holder": target.no, "lady_used": [*state.lady_used, target.no], "threads": threads}


async def assassinate_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """刺杀：蓝方拿满 3 个任务后，刺客在蓝方席位里指认梅林——命中红方翻盘，否则蓝方胜定。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    assassin = next(s for s in state.seats if s.role is Role.ASSASSIN)
    # 终局红方公开互认（官方规则：刺杀前红方共同商议，奥伯伦也现身）——
    # 刺客此刻拿到完整红方名单，指认范围因此恰为全部蓝方。
    evil_nos = [s.no for s in state.seats if s.role.side is Side.EVIL and s.no != assassin.no]
    good_nos = [s.no for s in state.seats if s.role.side is Side.GOOD]
    shooter = assassin.model_copy(update={"intel": evil_nos})

    view = _ask_view(deps, state, shooter, threads, "assassinate", with_situation=False)
    result, raw = await deps.player.assassinate(view)
    if result.target_no not in set(good_nos):
        raise ValueError(f"刺客 {assassin.no}号 刺向非法目标 {result.target_no}（只能指认蓝方：{good_nos}）")
    _commit(threads, assassin.no, view.prompt, raw)
    target = next(s for s in state.seats if s.no == result.target_no)
    hit = target.role is Role.MERLIN

    emit(
        "assassination",
        call_id=decision_id(state.quest_no, state.attempt, "assassinate", assassin.no),
        assassin_no=assassin.no,
        target_no=target.no,
        hit=hit,
        speech=result.speech,
        monologue=result.monologue,
    )
    _broadcast(threads, prompts.line_player(assassin.no, result.speech))
    deps.host.announce(
        threads, "assassination",
        prompts.line_host_assassination(assassin.no, target.no, hit),
        quest=state.quest_no, attempt=state.attempt,
    )
    return {"winner": "evil" if hit else "good", "win_reason": "assassination", "finished": True, "threads": threads}


async def postgame_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """赛后结算：揭晓全部角色与胜负，然后全员复盘——底牌已公开，round-robin 每人一段
    复盘感言（败方先说、胜方后说，组内按座位序）。串行不是摆设：前面人的复盘按序喂给后说者
    （ask_banter 的 prior），并关掉反从众提醒，让他们接梗/回怼/甩锅而不是各说各的。复盘原文
    只进事件流与 prior（不再 _broadcast 进线程，免得和 prior 块重复），吐槽/甩锅/自夸都是节目内容。"""
    emit(
        "game_end",
        winner=state.winner,
        win_reason=state.win_reason,
        quests=[
            {"quest": r.quest, "result": r.result, "fails": r.fails, "team": r.team}
            for r in state.history if r.result is not None
        ],
        seats=[{"no": s.no, "persona": s.persona_id, "role": s.role.value, "side": s.role.side.value, "model": s.model} for s in state.seats],
    )
    deps = _deps(config)
    threads = _threads_copy(state)
    deps.host.announce(
        threads, "reveal",
        prompts.line_host_reveal(state.winner, state.win_reason, {s.no: s.role for s in state.seats}),
        quest=state.quest_no, attempt=state.attempt,
    )
    speakers = sorted(state.seats, key=lambda s: (s.role.side.value == state.winner, s.no))
    said: list[tuple[int, str]] = []  # 已复盘者的 (座位号, 发言)，按序喂给后说者接话
    for seat in speakers:
        view = _ask_view(deps, state, seat, threads, "banter", with_situation=False, mimicry_guard=False)
        result, raw = await deps.player.banter(view)
        _commit(threads, seat.no, view.prompt, raw)
        said.append((seat.no, result.speech))
        emit(
            "banter",
            call_id=decision_id(state.quest_no, state.attempt, "banter", seat.no),
            seat_no=seat.no,
            role=seat.role.value,
            side=seat.role.side.value,
            text=result.speech,
            monologue=result.monologue,
        )
    deps.host.announce(threads, "closing", prompts.line_host_closing(), quest=state.quest_no, attempt=state.attempt)
    return {"threads": threads}


# ---------------------------------------------------------------- 路由

def route_after_vote(state: GameState) -> str:
    if state.finished:  # 第 5 次否决，红方获胜
        return "end"
    if sum(state.votes.values()) * 2 > len(state.seats):
        return "quest"
    return "repropose"


def route_after_check(state: GameState) -> str:
    if state.finished:  # 红方拿满 3 个失败任务
        return "end"
    if state.quests_succeeded >= rules.QUESTS_TO_WIN:
        return "assassinate"
    if state.lady_holder is not None and state.quest_no - 1 in rules.LADY_QUESTS:
        return "lady"  # 第 2/3/4 个任务刚结束且对局继续 → 触发查验
    return "continue"
