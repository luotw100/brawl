"""狼人杀裁判节点（纯代码）。AI 只决定"刀谁/验谁/救不救毒不毒/说什么/投谁/开不开枪"，
何时行动、谁死、怎么计票、判胜负全在这里。

无兜底：AI 输出非法（刀了不存在的目标、女巫无药却用药、投自己）一律抛错中断本局，让问题
立刻暴露，绝不静默替换。对局过程没有任何裁判侧随机——开局摇牌（角色）已前置到边界
validation.resolve_and_validate；放逐平票按规则即无人出局，狼刀分歧取多数（平则最小座位号）。

昼夜时序（修正版，贴合真人桌）：夜里狼刀→预言家→女巫各自暗行动，resolve_night 只结算不播报；
天亮后 dawn 公布死讯与翻牌，死者留遗言（last_words），随后死亡的猎人才开枪（hunter），最后
check 判胜负。枪在结算之前、在天亮之后——夜死的猎人不会在黑夜里抢先开枪。

线程纪律（同 avalon）：每席位 append-only 线程。公开记录灌全员 inbox：主持人串场经
host.announce（同时 emit host 事件、同源、将来逐句 TTS），玩家发言原文经 broadcast。轮到某席位
行动时把它 inbox flush 成一条 human、模型自由文本原文存为 assistant。狼队夜聊只进狼席位 inbox
（line_wolf_chat），好人永不可见。放逐投票真并发同时亮票，票型开齐后才广播。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from langchain_core.runnables import RunnableConfig

from core.events import decision_id, emit
from core.threads import SeatThread, broadcast, commit, copy_threads
from werewolf import prompts, rules
from werewolf.deps import Deps
from werewolf.player import PlayerView
from werewolf.registry import ModelSpec
from werewolf.state import DeathRecord, GameState, Role, Seat, SeerCheck, VoteRecord


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


def _threads_copy(state: GameState) -> dict[int, SeatThread]:
    return copy_threads(state.threads)


def _model_spec(deps: Deps, seat: Seat) -> ModelSpec:
    return deps.registry.models[seat.model]


def _wolf_allies(state: GameState, seat: Seat) -> list[int]:
    """狼人发牌时就知道的队友名单——不随死亡变化（队友死了你仍知道他是队友），
    故不按存活过滤；这样 system 前缀整局逐字不变、吃 prompt 缓存。"""
    if seat.role is not Role.WEREWOLF:
        return []
    return [s.no for s in state.seats if s.role is Role.WEREWOLF and s.no != seat.no]


def _ask_view(
    deps: Deps, state: GameState, seat: Seat, threads: dict[int, SeatThread], ask: str,
    *, night_target: int | None = None, with_situation: bool = True, mimicry_guard: bool = True,
) -> PlayerView:
    view = PlayerView(
        seat_no=seat.no,
        role=seat.role,
        model=_model_spec(deps, seat),
        players=len(state.seats),
        day=state.day,
        alive_nos=state.living_nos(),
        dead_nos=[s.no for s in state.seats if not s.alive],
        wolf_allies=_wolf_allies(state, seat),
        seer_checks=list(state.seer_checks) if seat.role is Role.SEER else [],
        witch_has_antidote=state.witch_has_antidote,
        witch_has_poison=state.witch_has_poison,
        night_target=night_target,
        tone=deps.registry.personas[seat.persona_id].tone,
        strategy=deps.registry.strategies[seat.strategy_id].system,
        messages=list(threads[seat.no].messages),
    )
    context = ""
    if with_situation:
        context = prompts.situation(view)
        private = prompts.private_block(view)
        if private:
            context += "\n" + private
    view.prompt = prompts.human_message(list(threads[seat.no].inbox), context, ask, view.tone, mimicry_guard=mimicry_guard)
    return view


def _apply_deaths(state: GameState, deaths: list[DeathRecord]) -> list[Seat]:
    dead = {d.seat_no for d in deaths}
    return [s.model_copy(update={"alive": False, "can_vote": False, "revealed": True}) if s.no in dead else s for s in state.seats]


def _win(state: GameState) -> tuple[str | None, str | None]:
    """屠边判负（纯函数）。狼全死→好人胜；神全灭或民全灭→狼胜。"""
    pairs = [(s.role, s.alive) for s in state.seats]
    if rules.living_wolves(pairs) == 0:
        return "good", "all_wolves_dead"
    if rules.living_gods(pairs) == 0:
        return "werewolf", "all_gods_dead"
    if rules.living_villagers(pairs) == 0:
        return "werewolf", "all_villagers_dead"
    return None, None


def _seats_payload(state: GameState) -> list[dict[str, Any]]:
    return [
        {"no": s.no, "persona": s.persona_id, "strategy": s.strategy_id, "role": s.role.value, "side": s.side.value, "model": s.model}
        for s in state.seats
    ]


# ---------------------------------------------------------------- 开局

async def opening_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """开局公布：把对局构成与全部底牌亮给观众（god-view）。角色已在边界摇定，这里不决定任何东西。
    身份不进任何席位 inbox——每个座位自己的角色已写在 system，别人的角色是秘密。"""
    deps = _deps(config)
    board = rules.board_by_id(state.board)
    threads = _threads_copy(state)
    payload = _seats_payload(state)
    emit("game_start", board=board.id, board_name=board.name, players=board.players, wolves=board.wolves, gods=board.gods, villagers=board.villagers, seats=payload)
    emit("deal", seats=payload)
    deps.host.announce(threads, "opening", prompts.line_host_opening(board.name, board.players, board.wolves), quest=state.day, attempt=1)
    return {"threads": threads}


# ---------------------------------------------------------------- 黑夜

async def night_start_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    threads = _threads_copy(state)
    day = state.day + 1 if state.phase == "after_vote" else state.day
    deps.host.announce(threads, "night_start", prompts.line_host_night(day), quest=day, attempt=1)
    emit("night_start", day=day)
    return {
        "day": day,
        "phase": "night",
        "round_deaths": [],
        "pending_hunter": None,
        "wolf_target": None,
        "witch_saved": False,
        "witch_poison_target": None,
        "threads": threads,
    }


async def wolf_kill_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """狼人共商刀口：存活狼人按座位序**串行**发言，每只狼能看到前面狼队友的夜聊（只进狼频道），
    最终刀口取全体提议的多数（平则取最小座位号）。串行而非并发，是为了让狼队真正协商、有戏可看。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    wolves = sorted((s for s in state.living() if s.role is Role.WEREWOLF), key=lambda s: s.no)
    candidates = [s.no for s in state.living() if s.role is not Role.WEREWOLF]
    prior: list[tuple[int, int, str]] = []
    payload: list[dict[str, Any]] = []
    targets: list[int] = []
    for wolf in wolves:
        view = _ask_view(deps, state, wolf, threads, prompts.ask_wolf_kill(candidates, prior))
        result, raw = await deps.player.wolf_kill(view)
        if result.target_no not in candidates:
            raise ValueError(f"狼人 {wolf.no}号 选择了非法刀口 {result.target_no}（可选：{candidates}）")
        commit(threads, wolf.no, view.prompt, raw)
        for other in wolves:  # 狼队夜聊只进其他狼席位 inbox，好人永不可见
            if other.no != wolf.no:
                threads[other.no].inbox.append(prompts.line_wolf_chat(wolf.no, result.target_no, result.speech))
        prior.append((wolf.no, result.target_no, result.speech))
        targets.append(result.target_no)
        payload.append({
            "seat_no": wolf.no, "target_no": result.target_no, "speech": result.speech,
            "monologue": result.monologue, "call_id": decision_id(state.day, 1, "wolf_kill", wolf.no),
        })
    counts = Counter(targets)
    target = min((-n, no) for no, n in counts.items())[1]
    emit("wolf_kill", day=state.day, target_no=target, votes=payload)
    return {"wolf_target": target, "threads": threads}


async def seer_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    seer = next((s for s in state.living() if s.role is Role.SEER), None)
    if seer is None:
        return {}
    threads = _threads_copy(state)
    candidates = [no for no in state.living_nos() if no != seer.no]
    view = _ask_view(deps, state, seer, threads, prompts.ask_seer(candidates))
    result, raw = await deps.player.seer_check(view)
    if result.target_no not in set(candidates):
        raise ValueError(f"预言家 {seer.no}号 查验了非法对象 {result.target_no}（可选：{candidates}）")
    target = state.seat(result.target_no)
    check = SeerCheck(target_no=target.no, side=target.side, day=state.day)
    commit(threads, seer.no, view.prompt, raw)
    threads[seer.no].inbox.append(f"（查验结果）{target.no}号是{rules.SIDE_LABEL[target.side]}。")
    emit(
        "seer_check", day=state.day, call_id=decision_id(state.day, 1, "seer_check", seer.no),
        seat_no=seer.no, target_no=target.no, result=target.side.value, monologue=result.monologue,
    )
    return {"seer_checks": [*state.seer_checks, check], "threads": threads}


async def witch_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    witch = next((s for s in state.living() if s.role is Role.WITCH), None)
    if witch is None:
        return {}
    threads = _threads_copy(state)
    candidates = [no for no in state.living_nos() if no != witch.no]
    view = _ask_view(
        deps, state, witch, threads,
        prompts.ask_witch(state.wolf_target, candidates, state.witch_has_antidote, state.witch_has_poison),
        night_target=state.wolf_target,
    )
    result, raw = await deps.player.witch_action(view)
    if result.save and (not state.witch_has_antidote or state.wolf_target is None):
        raise ValueError(f"女巫 {witch.no}号 非法使用解药（无药或今夜无刀口）")
    if result.poison_target_no is not None and (not state.witch_has_poison or result.poison_target_no not in set(candidates)):
        raise ValueError(f"女巫 {witch.no}号 非法使用毒药：{result.poison_target_no}（可选：{candidates}）")
    if result.save and result.poison_target_no == state.wolf_target:
        raise ValueError(f"女巫 {witch.no}号 同夜对 {state.wolf_target}号 又救又毒")
    commit(threads, witch.no, view.prompt, raw)
    emit(
        "witch_action", day=state.day, call_id=decision_id(state.day, 1, "witch_action", witch.no),
        seat_no=witch.no, save=result.save, poison_target_no=result.poison_target_no, monologue=result.monologue,
    )
    return {
        "witch_saved": result.save,
        "witch_poison_target": result.poison_target_no,
        "witch_has_antidote": state.witch_has_antidote and not result.save,
        "witch_has_poison": state.witch_has_poison and result.poison_target_no is None,
        "threads": threads,
    }


async def resolve_night_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """夜晚结算（纯计算，不播报）：算出今夜死者、标记盘面、定下待开枪的猎人。播报留给天亮的 dawn。"""
    deaths: list[DeathRecord] = []
    if state.wolf_target is not None and not state.witch_saved:
        t = state.seat(state.wolf_target)
        deaths.append(DeathRecord(seat_no=t.no, role=t.role, reason="wolf_kill", day=state.day))
    if state.witch_poison_target is not None:
        t = state.seat(state.witch_poison_target)
        deaths.append(DeathRecord(seat_no=t.no, role=t.role, reason="poison", day=state.day))
    deaths = list({d.seat_no: d for d in deaths}.values())  # 同一人被刀又被毒只记一次（保留先入的狼刀）
    seats = _apply_deaths(state, deaths)
    # 死于狼刀的猎人可开枪，死于毒不可
    hunter = next((d.seat_no for d in deaths if d.role is Role.HUNTER and d.reason != "poison"), None)
    return {
        "seats": seats,
        "deaths": [*state.deaths, *deaths],
        "round_deaths": deaths,
        "pending_hunter": hunter,
        "phase": "after_night",
    }


# ---------------------------------------------------------------- 天亮

async def dawn_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """天亮播报：主持人公布昨夜死讯与翻牌（口播同时进全员 inbox），并发结构化事件给观众侧。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    deaths = state.round_deaths
    deps.host.announce(threads, "day_start", prompts.line_host_dawn(state.day, [(d.seat_no, d.role) for d in deaths]), quest=state.day, attempt=1)
    emit("death_resolved", day=state.day, deaths=[d.model_dump(mode="json") for d in deaths])
    for d in deaths:
        emit("role_revealed", day=state.day, seat_no=d.seat_no, role=d.role.value, reason=d.reason)
    return {"threads": threads}


async def last_words_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """本轮死者留遗言（夜死者天亮后说、被放逐者放逐后说）。跳过待开枪的猎人——他在 hunter_node
    里以开枪发言作为临终发言，免得遗言和开枪两段重复。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    for death in state.round_deaths:
        if death.seat_no == state.pending_hunter:
            continue
        seat = state.seat(death.seat_no)
        view = _ask_view(deps, state, seat, threads, prompts.ask_last_words(death.reason))
        result, raw = await deps.player.last_words(view)
        commit(threads, seat.no, view.prompt, raw)
        broadcast(threads, prompts.line_player(seat.no, result.speech))
        emit(
            "speech", day=state.day, phase="last_words", call_id=decision_id(state.day, 1, "last_words", seat.no),
            seat_no=seat.no, text=result.speech, monologue=result.monologue,
        )
    return {"threads": threads}


async def hunter_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """待开枪的猎人开枪（可不开枪）。开枪发言即他的临终发言；命中者当场翻牌、加入本轮死者。"""
    if state.pending_hunter is None:
        return {}
    deps = _deps(config)
    threads = _threads_copy(state)
    hunter = state.seat(state.pending_hunter)
    candidates = state.living_nos()
    view = _ask_view(deps, state, hunter, threads, prompts.ask_hunter(candidates))
    result, raw = await deps.player.hunter_shoot(view)
    if result.target_no is not None and result.target_no not in set(candidates):
        raise ValueError(f"猎人 {hunter.no}号 开枪目标非法：{result.target_no}（可选：{candidates}）")
    commit(threads, hunter.no, view.prompt, raw)
    broadcast(threads, prompts.line_player(hunter.no, result.speech))
    deaths: list[DeathRecord] = []
    seats = state.seats
    victim_role: Role | None = None
    if result.target_no is not None:
        victim = state.seat(result.target_no)
        victim_role = victim.role
        death = DeathRecord(seat_no=victim.no, role=victim.role, reason="hunter_shot", day=state.day)
        seats = _apply_deaths(state, [death])
        deaths = [death]
    broadcast(threads, prompts.line_hunter_shot(hunter.no, result.target_no, victim_role))
    emit(
        "hunter_shot", day=state.day, call_id=decision_id(state.day, 1, "hunter_shoot", hunter.no),
        hunter_no=hunter.no, target_no=result.target_no, speech=result.speech, monologue=result.monologue,
    )
    for d in deaths:
        emit("role_revealed", day=state.day, seat_no=d.seat_no, role=d.role.value, reason=d.reason)
    return {
        "seats": seats,
        "deaths": [*state.deaths, *deaths],
        "round_deaths": [*state.round_deaths, *deaths],
        "pending_hunter": None,
        "threads": threads,
    }


# ---------------------------------------------------------------- 白天

async def discussion_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """白天发言轮：存活玩家按座位序逐个公开发言，后说的人能看到前面人的发言。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    for seat in state.living():
        view = _ask_view(deps, state, seat, threads, prompts.ask_discuss())
        result, raw = await deps.player.discuss(view)
        commit(threads, seat.no, view.prompt, raw)
        broadcast(threads, prompts.line_player(seat.no, result.speech))
        emit(
            "speech", day=state.day, phase="discussion", call_id=decision_id(state.day, 1, "discuss", seat.no),
            seat_no=seat.no, text=result.speech, monologue=result.monologue,
        )
    return {"threads": threads}


async def vote_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """放逐投票：有投票权的存活玩家**真并发同时**亮票（看不到别人这一轮的票），开齐后才公开票型。
    最高票唯一者出局，平票无人出局。计票结果存进 VoteRecord，由 resolve_vote 执行。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    voters = [s for s in state.living() if s.can_vote]
    sem = asyncio.Semaphore(deps.settings.concurrency)

    async def one(seat: Seat):
        view = _ask_view(deps, state, seat, threads, prompts.ask_vote([no for no in state.living_nos() if no != seat.no]))
        async with sem:
            result, raw = await deps.player.vote(view)
        return seat, view.prompt, result, raw

    results = await asyncio.gather(*(one(s) for s in voters))
    votes: dict[int, int] = {}
    payload: list[dict[str, Any]] = []
    living = set(state.living_nos())
    for seat, prompt, result, raw in results:
        if result.target_no not in living or result.target_no == seat.no:
            raise ValueError(f"{seat.no}号 投了非法对象 {result.target_no}（须存活且不能投自己）")
        votes[seat.no] = result.target_no
        payload.append({
            "seat_no": seat.no, "target_no": result.target_no, "monologue": result.monologue,
            "call_id": decision_id(state.day, 1, "vote", seat.no),
        })
        commit(threads, seat.no, prompt, raw)
    counts = Counter(votes.values())
    top = max(counts.values()) if counts else 0
    leaders = sorted(no for no, n in counts.items() if n == top)
    tied = len(leaders) != 1
    exiled = None if tied else leaders[0]
    broadcast(threads, prompts.line_vote_detail(votes))
    emit("vote_result", day=state.day, votes=payload, exiled=exiled, tied=tied)
    return {"votes": [*state.votes, VoteRecord(day=state.day, votes=votes, exiled=exiled, tied=tied)], "threads": threads}


async def resolve_vote_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """放逐结算：主持人公布结果。白痴首次被放逐翻牌免死、失投票权；已翻牌白痴再被投则放逐无效；
    其余正常出局翻牌、若是猎人则待开枪。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    last = state.votes[-1]
    if last.exiled is None:
        deps.host.announce(threads, "vote_result", prompts.line_host_vote_result(None, None, last.tied), quest=state.day, attempt=1)
        return {"round_deaths": [], "pending_hunter": None, "phase": "after_vote", "threads": threads}
    target = state.seat(last.exiled)
    if target.role is Role.IDIOT:
        if not target.revealed:
            seats = [s.model_copy(update={"revealed": True, "can_vote": False}) if s.no == target.no else s for s in state.seats]
            deps.host.announce(threads, "vote_result", prompts.line_host_idiot(target.no), quest=state.day, attempt=1)
            emit("idiot_revealed", day=state.day, seat_no=target.no)
            return {"seats": seats, "round_deaths": [], "pending_hunter": None, "phase": "after_vote", "threads": threads}
        broadcast(threads, prompts.line_idiot_immune(target.no))
        return {"round_deaths": [], "pending_hunter": None, "phase": "after_vote", "threads": threads}
    death = DeathRecord(seat_no=target.no, role=target.role, reason="exile", day=state.day)
    seats = _apply_deaths(state, [death])
    hunter = target.no if target.role is Role.HUNTER else None
    deps.host.announce(threads, "vote_result", prompts.line_host_vote_result(target.no, target.role, False), quest=state.day, attempt=1)
    emit("role_revealed", day=state.day, seat_no=target.no, role=target.role.value, reason="exile")
    return {
        "seats": seats,
        "deaths": [*state.deaths, death],
        "round_deaths": [death],
        "pending_hunter": hunter,
        "phase": "after_vote",
        "threads": threads,
    }


# ---------------------------------------------------------------- 判负 / 收官

async def check_win_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    winner, reason = _win(state)
    if winner is None:
        return {}
    return {"winner": winner, "win_reason": reason, "finished": True}


async def postgame_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    """赛后揭晓全部身份，然后全员复盘：败方先说、胜方后说，组内按座位序；前面人的复盘按序喂给
    后说者（ask_banter 的 prior），关掉反从众提醒，让他们接梗/回怼/甩锅。"""
    deps = _deps(config)
    threads = _threads_copy(state)
    emit(
        "game_end", winner=state.winner, win_reason=state.win_reason, day=state.day,
        seats=[{"no": s.no, "persona": s.persona_id, "role": s.role.value, "side": s.side.value, "alive": s.alive, "model": s.model} for s in state.seats],
        deaths=[d.model_dump(mode="json") for d in state.deaths],
    )
    deps.host.announce(threads, "reveal", prompts.line_host_reveal(state.winner or "?", state.win_reason or "?", {s.no: s.role for s in state.seats}), quest=state.day, attempt=1)
    speakers = sorted(state.seats, key=lambda s: (s.side.value == state.winner, s.no))
    said: list[tuple[int, str]] = []
    for seat in speakers:
        view = _ask_view(deps, state, seat, threads, prompts.ask_banter(said), with_situation=False, mimicry_guard=False)
        result, raw = await deps.player.banter(view)
        commit(threads, seat.no, view.prompt, raw)
        said.append((seat.no, result.speech))
        emit(
            "banter", call_id=decision_id(state.day, 1, "banter", seat.no),
            seat_no=seat.no, role=seat.role.value, side=seat.side.value, text=result.speech, monologue=result.monologue,
        )
    deps.host.announce(threads, "closing", prompts.line_host_closing(), quest=state.day, attempt=1)
    return {"threads": threads}


# ---------------------------------------------------------------- 路由

def route_after_hunter(state: GameState) -> str:
    return "hunter" if state.pending_hunter is not None else "check"


def route_after_check(state: GameState) -> str:
    if state.finished:
        return "end"
    return "day" if state.phase == "after_night" else "night"
