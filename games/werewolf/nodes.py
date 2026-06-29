"""狼人杀裁判节点。"""

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
from werewolf.state import DeathRecord, GameState, Role, Seat, SeerCheck, SpeechRecord, VoteRecord


def _deps(config: RunnableConfig) -> Deps:
    return config["configurable"]["deps"]


def _threads_copy(state: GameState) -> dict[int, SeatThread]:
    return copy_threads(state.threads)


def _model_spec(deps: Deps, seat: Seat) -> ModelSpec:
    return deps.registry.models[seat.model]


def _wolf_allies(state: GameState, seat: Seat) -> list[int]:
    if seat.role is not Role.WEREWOLF:
        return []
    return [s.no for s in state.seats if s.alive and s.role is Role.WEREWOLF and s.no != seat.no]


def _seer_checks_for(state: GameState, seat: Seat) -> list[SeerCheck]:
    if seat.role is not Role.SEER:
        return []
    return list(state.seer_checks)


def _ask_view(deps: Deps, state: GameState, seat: Seat, threads: dict[int, SeatThread], ask: str, night_target: int | None = None) -> PlayerView:
    view = PlayerView(
        seat_no=seat.no,
        role=seat.role,
        model=_model_spec(deps, seat),
        players=len(state.seats),
        day=state.day,
        alive_nos=state.living_nos(),
        dead_nos=[s.no for s in state.seats if not s.alive],
        wolf_allies=_wolf_allies(state, seat),
        seer_checks=_seer_checks_for(state, seat),
        witch_has_antidote=state.witch_has_antidote,
        witch_has_poison=state.witch_has_poison,
        night_target=night_target,
        tone=deps.registry.personas[seat.persona_id].tone,
        strategy=deps.registry.strategies[seat.strategy_id].system,
        messages=list(threads[seat.no].messages),
    )
    view.prompt = prompts.human_message(list(threads[seat.no].inbox), prompts.situation(view), ask, view.tone)
    return view


def _living_role_pairs(state: GameState) -> list[tuple[Role, bool]]:
    return [(s.role, s.alive) for s in state.seats]


def _win(state: GameState) -> tuple[str | None, str | None]:
    pairs = _living_role_pairs(state)
    wolves = rules.living_wolves(pairs)
    gods = rules.living_gods(pairs)
    villagers = rules.living_villagers(pairs)
    if wolves == 0:
        return "good", "all_wolves_dead"
    if gods == 0:
        return "werewolf", "all_gods_dead"
    if villagers == 0:
        return "werewolf", "all_villagers_dead"
    return None, None


def _apply_deaths(state: GameState, deaths: list[DeathRecord]) -> list[Seat]:
    dead = {d.seat_no for d in deaths}
    return [s.model_copy(update={"alive": False, "can_vote": False, "revealed": True}) if s.no in dead else s for s in state.seats]


async def opening_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    board = rules.board_by_id(state.board)
    threads = _threads_copy(state)
    seats_payload = [
        {"no": s.no, "persona": s.persona_id, "strategy": s.strategy_id, "role": s.role.value, "side": s.side.value, "model": s.model}
        for s in state.seats
    ]
    for seat in state.seats:
        threads[seat.no].inbox.append(prompts.line_private_role(seat.role, _wolf_allies(state, seat)))
    emit(
        "game_start",
        board=board.id,
        board_name=board.name,
        players=board.players,
        wolves=board.wolves,
        gods=board.gods,
        villagers=board.villagers,
        seats=seats_payload,
    )
    emit("deal", seats=seats_payload)
    deps.host.announce(threads, "opening", prompts.line_opening(board.name), quest=state.day, attempt=state.attempt)
    return {"threads": threads}


async def night_start_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    threads = _threads_copy(state)
    day = state.day + 1 if state.phase == "after_vote" else state.day
    deps.host.announce(threads, "night_start", prompts.line_night_start(day), quest=day, attempt=1)
    emit("night_start", day=day)
    return {
        "day": day,
        "phase": "opening",
        "round_deaths": [],
        "pending_hunter": None,
        "wolf_votes": {},
        "wolf_target": None,
        "witch_saved": False,
        "witch_poison_target": None,
        "threads": threads,
    }


async def wolf_kill_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    threads = _threads_copy(state)
    wolves = [s for s in state.seats if s.alive and s.role is Role.WEREWOLF]
    sem = asyncio.Semaphore(deps.settings.concurrency)

    async def one(seat: Seat):
        view = _ask_view(deps, state, seat, threads, prompts.ask_wolf_kill([s.no for s in state.living() if s.role is not Role.WEREWOLF]))
        async with sem:
            result, raw = await deps.player.wolf_kill(view)
        return seat, view.prompt, result, raw

    results = await asyncio.gather(*(one(s) for s in wolves))
    targets: dict[int, int] = {}
    speeches = []
    candidates = {s.no for s in state.living() if s.role is not Role.WEREWOLF}
    for seat, prompt, result, raw in results:
        if result.target_no not in candidates:
            raise ValueError(f"狼人 {seat.no}号 选择了非法刀口 {result.target_no}（可选：{sorted(candidates)}）")
        targets[seat.no] = result.target_no
        speeches.append({"seat_no": seat.no, "target_no": result.target_no, "speech": result.speech, "monologue": result.monologue})
        commit(threads, seat.no, prompt, raw)
    counts = Counter(targets.values())
    target = min((-n, no) for no, n in counts.items())[1]
    emit("wolf_kill", day=state.day, target_no=target, votes=speeches)
    return {"wolf_votes": targets, "wolf_target": target, "threads": threads}


async def seer_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    seer = next((s for s in state.seats if s.alive and s.role is Role.SEER), None)
    if seer is None:
        return {}
    threads = _threads_copy(state)
    candidates = [s.no for s in state.living() if s.no != seer.no]
    view = _ask_view(deps, state, seer, threads, prompts.ask_seer(candidates))
    result, raw = await deps.player.seer_check(view)
    if result.target_no not in set(candidates):
        raise ValueError(f"预言家 {seer.no}号 查验了非法对象 {result.target_no}（可选：{candidates}）")
    target = state.seat(result.target_no)
    check = SeerCheck(target_no=target.no, side=target.side, day=state.day)
    commit(threads, seer.no, view.prompt, raw)
    threads[seer.no].inbox.append(f"查验结果：{target.no}号是{rules.SIDE_LABEL[target.side]}。")
    emit(
        "seer_check",
        day=state.day,
        call_id=decision_id(state.day, 1, "seer_check", seer.no),
        seat_no=seer.no,
        target_no=target.no,
        result=target.side.value,
        monologue=result.monologue,
    )
    return {"last_seer_check": check, "seer_checks": [*state.seer_checks, check], "threads": threads}


async def witch_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    witch = next((s for s in state.seats if s.alive and s.role is Role.WITCH), None)
    if witch is None:
        return {}
    threads = _threads_copy(state)
    candidates = [s.no for s in state.living() if s.no != witch.no]
    view = _ask_view(
        deps,
        state,
        witch,
        threads,
        prompts.ask_witch(state.wolf_target, candidates, state.witch_has_antidote, state.witch_has_poison),
        night_target=state.wolf_target,
    )
    result, raw = await deps.player.witch_action(view)
    if result.save and (not state.witch_has_antidote or state.wolf_target is None):
        raise ValueError(f"女巫 {witch.no}号 非法使用解药")
    if result.poison_target_no is not None and (not state.witch_has_poison or result.poison_target_no not in set(candidates)):
        raise ValueError(f"女巫 {witch.no}号 非法使用毒药：{result.poison_target_no}（可选：{candidates}）")
    if result.save and result.poison_target_no == state.wolf_target:
        raise ValueError(f"女巫 {witch.no}号 同夜救毒同一人：{state.wolf_target}")
    commit(threads, witch.no, view.prompt, raw)
    emit(
        "witch_action",
        day=state.day,
        call_id=decision_id(state.day, 1, "witch_action", witch.no),
        seat_no=witch.no,
        save=result.save,
        poison_target_no=result.poison_target_no,
        monologue=result.monologue,
    )
    return {
        "witch_saved": result.save,
        "witch_poison_target": result.poison_target_no,
        "witch_has_antidote": state.witch_has_antidote and not result.save,
        "witch_has_poison": state.witch_has_poison and result.poison_target_no is None,
        "threads": threads,
    }


async def resolve_night_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deaths = []
    if state.wolf_target is not None and not state.witch_saved:
        target = state.seat(state.wolf_target)
        deaths.append(DeathRecord(seat_no=target.no, role=target.role, reason="wolf_kill", day=state.day))
    if state.witch_poison_target is not None:
        target = state.seat(state.witch_poison_target)
        deaths.append(DeathRecord(seat_no=target.no, role=target.role, reason="poison", day=state.day))
    unique = {d.seat_no: d for d in deaths}
    deaths = list(unique.values())
    seats = _apply_deaths(state, deaths)
    hunter = next((d.seat_no for d in deaths if d.role is Role.HUNTER and d.reason != "poison"), None)
    emit("death_resolved", day=state.day, deaths=[d.model_dump(mode="json") for d in deaths])
    return {"seats": seats, "deaths": [*state.deaths, *deaths], "round_deaths": deaths, "pending_hunter": hunter, "phase": "after_night"}


async def day_announce_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    threads = _threads_copy(state)
    death_nos = [d.seat_no for d in state.round_deaths]
    deps.host.announce(threads, "day_start", prompts.line_day_start(state.day, death_nos), quest=state.day, attempt=1)
    for d in state.round_deaths:
        line = prompts.line_death(d.seat_no, d.role, d.reason)
        broadcast(threads, line)
        emit("role_revealed", day=state.day, seat_no=d.seat_no, role=d.role.value, reason=d.reason)
    return {"threads": threads}


async def last_words_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    threads = _threads_copy(state)
    for death in state.round_deaths:
        seat = state.seat(death.seat_no)
        view = _ask_view(deps, state, seat, threads, prompts.ask_last_words(death.reason))
        result, raw = await deps.player.last_words(view)
        commit(threads, seat.no, view.prompt, raw)
        broadcast(threads, prompts.line_player(seat.no, result.speech))
        emit(
            "speech",
            day=state.day,
            phase="last_words",
            call_id=decision_id(state.day, 1, "last_words", seat.no),
            seat_no=seat.no,
            text=result.speech,
            monologue=result.monologue,
        )
    return {"threads": threads}


async def discussion_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    threads = _threads_copy(state)
    speeches = list(state.speeches)
    for seat in state.living():
        view = _ask_view(deps, state, seat, threads, prompts.ask_discuss())
        result, raw = await deps.player.discuss(view)
        commit(threads, seat.no, view.prompt, raw)
        line = prompts.line_player(seat.no, result.speech)
        broadcast(threads, line)
        speeches.append(SpeechRecord(day=state.day, seat_no=seat.no, text=result.speech))
        emit(
            "speech",
            day=state.day,
            phase="discussion",
            call_id=decision_id(state.day, 1, "discuss", seat.no),
            seat_no=seat.no,
            text=result.speech,
            monologue=result.monologue,
        )
    return {"speeches": speeches, "threads": threads}


async def vote_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
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
    votes = {}
    vote_payload = []
    living = set(state.living_nos())
    for seat, prompt, result, raw in results:
        if result.target_no not in living or result.target_no == seat.no:
            raise ValueError(f"{seat.no}号 投了非法对象 {result.target_no}（存活且不能投自己）")
        votes[seat.no] = result.target_no
        vote_payload.append({"seat_no": seat.no, "target_no": result.target_no, "monologue": result.monologue})
        commit(threads, seat.no, prompt, raw)
    counts = Counter(votes.values())
    top = max(counts.values())
    leaders = sorted(no for no, n in counts.items() if n == top)
    tied = len(leaders) > 1
    exiled = None if tied else leaders[0]
    line = prompts.line_vote_result(votes, exiled, tied)
    broadcast(threads, line)
    emit("vote_result", day=state.day, votes=vote_payload, exiled=exiled, tied=tied)
    return {"votes": [*state.votes, VoteRecord(day=state.day, votes=votes, exiled=exiled, tied=tied)], "threads": threads}


async def resolve_vote_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    last_vote = state.votes[-1]
    if last_vote.exiled is None:
        return {"round_deaths": [], "pending_hunter": None, "phase": "after_vote"}
    target = state.seat(last_vote.exiled)
    threads = _threads_copy(state)
    if target.role is Role.IDIOT and not target.revealed:
        seats = [s.model_copy(update={"revealed": True, "can_vote": False}) if s.no == target.no else s for s in state.seats]
        line = prompts.line_idiot_reveal(target.no)
        broadcast(threads, line)
        emit("idiot_revealed", day=state.day, seat_no=target.no)
        return {"seats": seats, "round_deaths": [], "pending_hunter": None, "phase": "after_vote", "threads": threads}
    death = DeathRecord(seat_no=target.no, role=target.role, reason="exile", day=state.day)
    seats = _apply_deaths(state, [death])
    hunter = target.no if target.role is Role.HUNTER else None
    line = prompts.line_death(target.no, target.role, "exile")
    broadcast(threads, line)
    emit("role_revealed", day=state.day, seat_no=target.no, role=target.role.value, reason="exile")
    return {
        "seats": seats,
        "deaths": [*state.deaths, death],
        "round_deaths": [death],
        "pending_hunter": hunter,
        "phase": "after_vote",
        "threads": threads,
    }


async def hunter_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
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
    deaths = []
    seats = state.seats
    if result.target_no is not None:
        target = state.seat(result.target_no)
        death = DeathRecord(seat_no=target.no, role=target.role, reason="hunter_shot", day=state.day)
        seats = _apply_deaths(state, [death])
        deaths = [death]
    line = prompts.line_hunter_shot(hunter.no, result.target_no)
    broadcast(threads, line)
    emit(
        "hunter_shot",
        day=state.day,
        call_id=decision_id(state.day, 1, "hunter_shoot", hunter.no),
        hunter_no=hunter.no,
        target_no=result.target_no,
        speech=result.speech,
        monologue=result.monologue,
    )
    return {"seats": seats, "deaths": [*state.deaths, *deaths], "round_deaths": [*state.round_deaths, *deaths], "pending_hunter": None, "threads": threads}


async def check_win_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    winner, reason = _win(state)
    if winner is None:
        return {}
    return {"winner": winner, "win_reason": reason, "finished": True}


async def postgame_node(state: GameState, config: RunnableConfig) -> dict[str, Any]:
    deps = _deps(config)
    threads = _threads_copy(state)
    emit(
        "game_end",
        winner=state.winner,
        win_reason=state.win_reason,
        day=state.day,
        seats=[
            {
                "no": s.no,
                "persona": s.persona_id,
                "role": s.role.value,
                "side": s.side.value,
                "alive": s.alive,
                "model": s.model,
            }
            for s in state.seats
        ],
        deaths=[d.model_dump(mode="json") for d in state.deaths],
    )
    deps.host.announce(threads, "game_end", prompts.line_game_end(state.winner or "?", state.win_reason or "?"), quest=state.day, attempt=1)
    return {"threads": threads}


def route_after_hunter(state: GameState) -> str:
    return "hunter" if state.pending_hunter is not None else "check"


def route_after_check(state: GameState) -> str:
    if state.finished:
        return "end"
    return "day" if state.phase == "after_night" else "night"
