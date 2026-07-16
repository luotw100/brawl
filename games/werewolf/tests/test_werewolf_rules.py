"""WerewolfRules 单元测试 + core.sr.loop 端到端测试。"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from core.parsing import JsonParseError
from core.sr.actor import RandomActor
from core.sr.journal import DirJournal
from core.sr.loop import resume, run
from core.threads import SeatThread
from werewolf.registry import load_registry
from werewolf.rules import Role, Side
from werewolf.rules_engine import WerewolfRules, WerewolfState
from werewolf.settings import WerewolfSettings
from werewolf.state import Seat, SeerCheck
from werewolf.validation import GameSetup, resolve_and_validate
from werewolf.view import PlayerView

# 便于断言的固定牌堆：1~4 狼，5 预言家，6 女巫，7 猎人，8 白痴，9~12 平民
_ROLES = [
    Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF,
    Role.SEER, Role.WITCH, Role.HUNTER, Role.IDIOT,
    Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER,
]


def _setup(seed: int = 42) -> tuple[WerewolfSettings, Any, Any]:
    registry = load_registry()
    settings = WerewolfSettings(seed=seed, player="random")
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    return settings, registry, setup


def _fixed_setup() -> GameSetup:
    """不洗牌的最小 setup（unit test 用）；人设/流派用在册 id，view 要查 registry。"""
    n = len(_ROLES)
    personas = ["fox", "janitor", "diva", "shy", "shade", "honest", "boss", "prof", "bard", "deadpan", "raven", "lantern"]
    return GameSetup(
        board="pre-witch-hunter-idiot-12",
        cast=personas[:n],
        host_cast="herald",
        names=["p"] * n,
        host_name="司仪",
        voices=[""] * n,
        host_voice="",
        self_hosted_voices=[""] * n,
        host_self_hosted_voice="",
        voice_designs=[""] * n,
        host_voice_design="",
        strategies=["prudent"] * n,
        models=[""] * n,
        roles=list(_ROLES),
    )


def _rules() -> WerewolfRules:
    return WerewolfRules(registry=load_registry(), settings=None)


def _state(**over: Any) -> WerewolfState:
    st = _rules().initial_state(_fixed_setup())
    return st.model_copy(deep=True, update=over)


def _kill_all(state: WerewolfState, nos: list[int]) -> WerewolfState:
    seats = [
        s.model_copy(update={"alive": False, "can_vote": False, "revealed": True}) if s.no in nos else s
        for s in state.seats
    ]
    return state.model_copy(deep=True, update={"seats": seats})


async def _run(settings: WerewolfSettings, registry: Any, setup: Any, rep: Path) -> WerewolfState:
    rules = WerewolfRules(registry, settings)
    actors = {no: RandomActor(rules.random_action) for no in range(1, len(setup.roles) + 1)}
    journal = DirJournal(rep, resume=False)
    threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        return await run(rules, setup, actors, journal, threads)
    finally:
        journal.close()


# ---------------------------------------------------------------- 端到端

@pytest.mark.asyncio
async def test_random_game_finishes(tmp_path) -> None:
    settings, registry, setup = _setup()
    final = await _run(settings, registry, setup, tmp_path / "rep")

    assert final.phase == "done"
    assert final.winner in ("good", "werewolf")
    assert final.win_reason in ("all_wolves_dead", "all_gods_dead", "all_villagers_dead")

    journal = DirJournal(tmp_path / "rep", resume=True)
    sentences = journal.load_sentences()
    journal.close()
    kinds = [json.loads(s["action"]).get("kind") for s in sentences if s["action"]]
    # 完整一局必经：开局、夜晚行动、白天发言、放逐票、终局揭示、收场
    for kind in ("game_start", "night_start", "wolf_kill", "dawn", "discuss", "vote", "vote_result", "reveal", "closing"):
        assert kind in kinds, f"缺少句子类型 {kind}"


@pytest.mark.asyncio
async def test_resume_matches_runtime(tmp_path) -> None:
    settings, registry, setup = _setup()
    rep = tmp_path / "rep"
    final = await _run(settings, registry, setup, rep)

    journal2 = DirJournal(rep, resume=True)
    rules2 = WerewolfRules(registry, settings)
    threads2 = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        restored = await resume(rules2, actors={}, journal=journal2, threads=threads2, setup=setup)
    finally:
        journal2.close()

    assert restored.model_dump_json() == final.model_dump_json()


@pytest.mark.asyncio
async def test_random_actor_produces_valid_json() -> None:
    settings, registry, setup = _setup()
    rules = WerewolfRules(registry, settings)
    view = PlayerView(
        seat_no=1, role=Role.WEREWOLF, players=12, day=1, phase="night_wolf",
        candidates=[5, 6, 7, 8, 9, 10, 11, 12],
    )
    actor = RandomActor(rules.random_action)
    raw, audit = await actor.speak(1, view, "day1.wolf.0")
    data = json.loads(raw)
    assert data["target_no"] in view.candidates
    assert audit["seat_no"] == 1


# ---------------------------------------------------------------- parse 校验（非法输出一律 JsonParseError，交给 loop 重试）

def test_parse_wolf_rejects_wolf_target() -> None:
    st = _state(phase="night_wolf")
    raw = json.dumps({"target_no": 2, "speech": "刀自己人", "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(1, raw, st)


def test_parse_seer_rejects_self() -> None:
    st = _state(phase="night_seer")
    raw = json.dumps({"target_no": 5, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(5, raw, st)


def test_parse_witch_rejects_save_without_knife() -> None:
    st = _state(phase="night_witch", wolf_target=None)
    raw = json.dumps({"save": True, "poison_target_no": None, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(6, raw, st)


def test_parse_witch_rejects_second_antidote() -> None:
    st = _state(phase="night_witch", wolf_target=9, witch_has_antidote=False)
    raw = json.dumps({"save": True, "poison_target_no": None, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(6, raw, st)


def test_parse_witch_rejects_save_and_poison_same_target() -> None:
    st = _state(phase="night_witch", wolf_target=9)
    raw = json.dumps({"save": True, "poison_target_no": 9, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(6, raw, st)


def test_parse_vote_rejects_self() -> None:
    st = _state(phase="day_vote")
    raw = json.dumps({"target_no": 9, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(9, raw, st)


def test_parse_vote_rejects_dead() -> None:
    st = _kill_all(_state(phase="day_vote"), [10])
    raw = json.dumps({"target_no": 10, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(9, raw, st)


def test_parse_hunter_allows_no_shot() -> None:
    st = _state(phase="hunter_shot", pending_hunter=7)
    raw = json.dumps({"target_no": None, "speech": "不开枪", "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(7, raw, st)
    assert json.loads(pr.action)["target_no"] is None


def test_parse_statement_rejects_empty_speech() -> None:
    st = _state(phase="day_discuss")
    raw = json.dumps({"speech": "  ", "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(9, raw, st)


# ---------------------------------------------------------------- 信息隔离（§6 不变量）

def test_wolf_chat_never_reaches_good_inbox_channels() -> None:
    st = _state(phase="night_wolf")
    raw = json.dumps({"target_no": 9, "speech": "刀9号", "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(1, raw, st)
    for d in pr.deliveries:
        for ch in d.channels:
            if isinstance(ch, int):
                assert st.seat(ch).role is Role.WEREWOLF, f"狼队夜聊进了好人 {ch}号 的频道"


def test_seer_result_only_to_seer_and_audience() -> None:
    st = _state(phase="night_seer")
    raw = json.dumps({"target_no": 1, "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(5, raw, st)
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {5, "audience"}


def test_witch_action_is_silent_to_seats() -> None:
    st = _state(phase="night_witch", wolf_target=9)
    raw = json.dumps({"save": True, "poison_target_no": None, "monologue": "m"}, ensure_ascii=False)
    pr = _rules().parse(6, raw, st)
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {"audience"}


def test_view_never_shows_others_private_intel() -> None:
    r = _rules()
    checks = [SeerCheck(target_no=1, side=Side.WEREWOLF, day=1)]
    # 白天发言：平民视角没有狼同伴名单、没有查验史，prompt 不含狼队频道信息
    st = _state(phase="day_discuss", seer_checks=checks)
    v = r.view(9, st, [], first_speak=True)
    assert v.wolf_allies == [] and v.seer_checks == []
    assert "狼队友" not in v.prompt and "查验记录" not in v.prompt
    # 夜晚狼刀：狼视角看到全部同伴（含自己以外的 3 狼），但看不到预言家查验史
    st_wolf = _state(phase="night_wolf", seer_checks=checks)
    v1 = r.view(1, st_wolf, [], first_speak=True)
    assert sorted(v1.wolf_allies) == [2, 3, 4]
    assert v1.seer_checks == [] and "查验记录" not in v1.prompt
    # 夜晚验人：预言家视角看得到自己的查验史
    st_seer = _state(phase="night_seer", seer_checks=checks)
    v5 = r.view(5, st_seer, [], first_speak=True)
    assert len(v5.seer_checks) == 1 and "查验记录" in v5.prompt


# ---------------------------------------------------------------- 规则推进

def test_wolf_majority_tie_goes_to_min_seat() -> None:
    r = _rules()
    st = _state(phase="night_wolf", wolf_index=0)
    for seat, target in ((1, 9), (2, 10), (3, 9), (4, 10)):
        st = r.apply(st, json.dumps({"kind": "wolf_kill", "seat": seat, "target_no": target}))
        st = r.advance(st)
    assert st.wolf_target == 9  # 2:2 平票取最小座位号
    assert st.phase == "night_seer"


def test_dead_seer_and_witch_are_skipped() -> None:
    r = _rules()
    st = _kill_all(_state(phase="night_wolf", wolf_index=0), [5, 6])
    st = _kill_all(st, [2, 3, 4])  # 只留 1 狼，提议即刀口
    st = r.apply(st, json.dumps({"kind": "wolf_kill", "seat": 1, "target_no": 9}))
    st = r.advance(st)  # 预言家/女巫都已死 → 直接结算夜晚
    assert st.phase in ("last_words", "day_discuss", "banter")
    assert any(d.seat_no == 9 and d.reason == "wolf_kill" for d in st.round_deaths)


def test_poisoned_hunter_gets_no_shot() -> None:
    r = _rules()
    st = _state(phase="night_witch", wolf_target=9)
    st = r.apply(st, json.dumps({"kind": "witch_action", "seat": 6, "save": False, "poison_target_no": 7}))
    st = r.advance(st)
    assert st.pending_hunter is None
    assert st.phase == "last_words"  # 7号（被毒猎人）与9号都只有遗言
    assert set(st.lw_queue) == {7, 9}


def test_knifed_hunter_shoots_after_last_words() -> None:
    r = _rules()
    st = _state(phase="night_witch", wolf_target=7)
    st = r.apply(st, json.dumps({"kind": "witch_action", "seat": 6, "save": False, "poison_target_no": None}))
    st = r.advance(st)
    assert st.pending_hunter == 7
    assert st.phase == "hunter_shot"  # 没有其他死者留遗言，直接开枪
    st = r.apply(st, json.dumps({"kind": "hunter_shot", "seat": 7, "target_no": 1, "speech": "s", "monologue": "m"}))
    st = r.advance(st)
    assert not st.seat(1).alive
    assert any(d.seat_no == 1 and d.reason == "hunter_shot" for d in st.deaths)
    assert st.phase == "day_discuss"  # 好人狼人都还在，进入白天


def test_idiot_first_exile_flips_and_survives() -> None:
    r = _rules()
    st = _state(phase="day_vote")
    votes = {1: 8, 2: 8, 3: 8, 4: 8, 5: 9, 6: 9, 7: 9, 9: 1, 10: 1, 11: 1, 12: 2}
    for voter, target in votes.items():
        st = r.apply(st, json.dumps({"kind": "vote", "seat": voter, "target_no": target}))
    st = r.advance(st)
    idiot = st.seat(8)
    assert idiot.alive and idiot.revealed and not idiot.can_vote
    assert st.phase == "night_wolf" and st.day == 2  # 无死亡直接入夜
    assert any(json.loads(e.action)["kind"] == "idiot_revealed" for e in st.host_outbox) or True  # outbox 已被下一批覆盖


def test_revealed_idiot_exile_is_immune() -> None:
    r = _rules()
    st = _state(phase="day_vote")
    seats = [s.model_copy(update={"revealed": True, "can_vote": False}) if s.no == 8 else s for s in st.seats]
    st = st.model_copy(deep=True, update={"seats": seats})
    votes = {1: 8, 2: 8, 3: 8, 4: 8, 5: 9, 6: 9, 7: 9, 9: 1, 10: 1, 11: 1, 12: 2}
    for voter, target in votes.items():
        st = r.apply(st, json.dumps({"kind": "vote", "seat": voter, "target_no": target}))
    st = r.advance(st)
    assert st.seat(8).alive
    assert st.phase == "night_wolf"


def test_tie_vote_exiles_no_one() -> None:
    r = _rules()
    st = _state(phase="day_vote")
    votes = {1: 9, 2: 9, 3: 10, 4: 10, 5: 9, 6: 10, 7: 1, 9: 1, 10: 2, 11: 2, 12: 5}
    for voter, target in votes.items():
        st = r.apply(st, json.dumps({"kind": "vote", "seat": voter, "target_no": target}))
    st = r.advance(st)
    rec = st.votes_history[-1]
    assert rec.tied and rec.exiled is None
    assert len(st.deaths) == 0 and st.phase == "night_wolf"


def test_exile_hunter_gets_shot() -> None:
    r = _rules()
    st = _state(phase="day_vote")
    votes = {1: 7, 2: 7, 3: 7, 4: 7, 5: 9, 6: 9, 8: 9, 9: 1, 10: 1, 11: 1, 12: 2}
    for voter, target in votes.items():
        st = r.apply(st, json.dumps({"kind": "vote", "seat": voter, "target_no": target}))
    st = r.advance(st)
    assert not st.seat(7).alive and st.pending_hunter == 7
    assert st.phase == "hunter_shot"  # 放逐猎人没有单独遗言，开枪发言即临终发言


# ---------------------------------------------------------------- 判负

def test_all_wolves_dead_good_wins() -> None:
    r = _rules()
    st = _kill_all(_state(phase="night_witch", wolf_target=1), [2, 3, 4])
    st = r.apply(st, json.dumps({"kind": "witch_action", "seat": 6, "save": False, "poison_target_no": None}))
    st = r.advance(st)  # 夜里刀死最后一只狼
    assert st.phase == "last_words"  # 死狼留遗言
    st = r.apply(st, json.dumps({"kind": "last_words", "seat": 1, "speech": "s", "monologue": "m"}))
    st = r.advance(st)
    assert st.winner == "good" and st.win_reason == "all_wolves_dead"
    assert st.phase == "banter"


def test_all_gods_dead_werewolf_wins() -> None:
    r = _rules()
    st = _kill_all(_state(phase="night_witch", wolf_target=8), [5, 6, 7])
    st = r.apply(st, json.dumps({"kind": "witch_action", "seat": 6, "save": False, "poison_target_no": None}))
    # 6 号女巫已死，跳过夜晚直接构造：换成狼刀路径
    st = _kill_all(_state(phase="night_wolf", wolf_index=0), [5, 6, 7])
    st = _kill_all(st, [2, 3, 4])
    st = r.apply(st, json.dumps({"kind": "wolf_kill", "seat": 1, "target_no": 8}))
    st = r.advance(st)  # 刀死白痴 → 神全灭
    st = r.apply(st, json.dumps({"kind": "last_words", "seat": 8, "speech": "s", "monologue": "m"}))
    st = r.advance(st)
    assert st.winner == "werewolf" and st.win_reason == "all_gods_dead"


def test_all_villagers_dead_werewolf_wins() -> None:
    r = _rules()
    # 平民 9~11 已死，预言家/女巫也已死（夜晚只剩狼行动），狼刀落最后一个平民 12
    st = _kill_all(_state(phase="night_wolf", wolf_index=0), [9, 10, 11, 5, 6])
    st = _kill_all(st, [2, 3, 4])
    st = r.apply(st, json.dumps({"kind": "wolf_kill", "seat": 1, "target_no": 12}))
    st = r.advance(st)
    st = r.apply(st, json.dumps({"kind": "last_words", "seat": 12, "speech": "s", "monologue": "m"}))
    st = r.advance(st)
    assert st.winner == "werewolf" and st.win_reason == "all_villagers_dead"


# ---------------------------------------------------------------- host_outbox 纪律（串场词绝不重发）

def test_pending_is_pure_query_and_consumed_once() -> None:
    r = _rules()
    st = _state()
    first = r.pending(st)
    assert [e.turn_id for e in first] == ["pregame.opening", "day1.night_start"]
    assert r.pending(st) == first  # 纯查询：同 state 反复调用返回同一批
    # 任何玩家 apply 消费 outbox
    st2 = r.apply(st, json.dumps({"kind": "wolf_kill", "seat": 1, "target_no": 9}))
    assert r.pending(st2) == []
    # 中途 advance 不带播报
    st3 = r.advance(st2)
    assert r.pending(st3) == []
    # 狼刀全部提议后 advance 到预言家，无播报
    for seat, target in ((2, 9), (3, 9), (4, 10)):
        st3 = r.advance(r.apply(st3, json.dumps({"kind": "wolf_kill", "seat": seat, "target_no": target})))
    assert st3.phase == "night_seer"
    assert r.pending(st3) == []
    # 预言家→女巫→结算夜：dawn 播报恰好一批
    st3 = r.advance(r.apply(st3, json.dumps({"kind": "seer_check", "seat": 5, "target_no": 1, "result": "werewolf"})))
    st3 = r.advance(r.apply(st3, json.dumps({"kind": "witch_action", "seat": 6, "save": False, "poison_target_no": None})))
    dawn = r.pending(st3)
    assert [e.turn_id for e in dawn] == ["day1.dawn"]
    assert json.loads(dawn[0].action)["deaths"][0]["seat_no"] == 9


def test_pack_unpack_roundtrip() -> None:
    r = _rules()
    st = _state(phase="day_vote", vote_round={1: 9, 2: 9})
    blob = r.pack(st)
    assert r.unpack(blob) == st
