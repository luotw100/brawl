"""WerewolfRules 单元测试 + core.runtime.loop 端到端测试。"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from core.runtime.actor import RandomActor
from core.runtime.journal import DirJournal
from core.runtime.loop import resume, run
from core.runtime.parsing import JsonParseError
from core.runtime.threads import SeatThread, ThreadMessage
from werewolf import prompts, rules
from werewolf.registry import load_registry
from werewolf.rules import Role, Side
from werewolf.rules_engine import WerewolfRules, WerewolfState
from werewolf.settings import WerewolfSettings
from werewolf.state import DeathRecord, SeerCheck
from werewolf.validation import GameSetup, resolve_and_validate
from werewolf.view import PlayerView

# 直接使用生产 BoardSpec，避免测试牌堆与真实板子各自漂移。
_ROLES = list(rules.PRE_WITCH_HUNTER_IDIOT_12.roles)


def _setup(seed: int = 42) -> tuple[WerewolfSettings, Any, Any]:
    registry = load_registry()
    settings = WerewolfSettings(seed=seed, player="random")
    rng = random.Random(settings.seed)
    setup = resolve_and_validate(settings, registry, rng)
    return settings, registry, setup


def _fixed_setup() -> GameSetup:
    """不洗牌的最小完整 setup（unit test 用）。"""
    n = len(_ROLES)
    personas = ["fox", "janitor", "diva", "shy", "shade", "honest", "boss", "prof", "bard", "deadpan", "raven", "lantern"]
    return GameSetup(
        board="pre-witch-hunter-idiot-12",
        cast=tuple(personas[:n]),
        host_cast="herald",
        names=tuple(f"p{seat}" for seat in range(1, n + 1)),
        host_name="司仪",
        voices=("test-voice",) * n,
        host_voice="test-host-voice",
        voice_designs=("",) * n,
        host_voice_design="",
        tones=("测试腔调",) * n,
        strategies=("prudent",) * n,
        strategy_systems=("测试流派",) * n,
        models=("test-model",) * n,
        opencode_models=("",) * n,
        roles=tuple(_ROLES),
    )


def _rules() -> WerewolfRules:
    return WerewolfRules()


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
    rules = WerewolfRules()
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
    for kind in (
        "game_start",
        "night_start",
        "wolf_kill",
        "wolf_kill_result",
        "witch_result",
        "dawn",
        "discuss",
        "vote",
        "vote_result",
        "reveal",
        "closing",
    ):
        assert kind in kinds, f"缺少句子类型 {kind}"

    # 查验反馈是裁判私密回话，不能冒充预言家说话。
    seer_results = [s for s in sentences if s["action"] and json.loads(s["action"]).get("kind") == "seer_result"]
    assert seer_results and all(s["speaker"] == "host" for s in seer_results)


@pytest.mark.asyncio
async def test_resume_matches_runtime(tmp_path) -> None:
    settings, registry, setup = _setup()
    rep = tmp_path / "rep"
    final = await _run(settings, registry, setup, rep)

    journal2 = DirJournal(rep, resume=True)
    rules2 = WerewolfRules()
    threads2 = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        restored = await resume(rules2, journal=journal2, threads=threads2, setup=setup)
    finally:
        journal2.close()

    assert restored.model_dump_json() == final.model_dump_json()


@pytest.mark.asyncio
async def test_midgame_crash_resume_reproduces_identical_log(tmp_path) -> None:
    """中段崩溃后续跑：确定性重放整局，日志逐行一致、host_outbox 串场零重复。

    狼人杀 host 串场全走 host_outbox：续跑恢复后 outbox 由重放重建，dawn/night_start
    等播报在崩溃前已落盘——按 turn_id 去重后不得重记。
    """
    settings, registry, setup = _setup()
    rep = tmp_path / "rep"
    final = await _run(settings, registry, setup, rep)
    original = (rep / "sentences.jsonl").read_text(encoding="utf-8").splitlines()

    rows = [json.loads(line) for line in original]
    cut_commit = rows[len(rows) // 2]["commit_id"]
    cut = next(index for index, row in enumerate(rows) if row["commit_id"] == cut_commit)
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    prefix = "\n".join(original[:cut])
    (crashed / "sentences.jsonl").write_text(f"{prefix}\n" if prefix else "", encoding="utf-8")

    rules2 = WerewolfRules()
    journal = DirJournal(crashed, resume=True)
    threads = {no: SeatThread() for no in range(1, len(setup.roles) + 1)}
    try:
        actors = {no: RandomActor(rules2.random_action) for no in range(1, len(setup.roles) + 1)}
        final2 = await run(rules2, setup, actors, journal, threads)
    finally:
        journal.close()

    resumed = (crashed / "sentences.jsonl").read_text(encoding="utf-8").splitlines()
    assert resumed == original
    assert final2.model_dump_json() == final.model_dump_json()


@pytest.mark.asyncio
async def test_random_actor_produces_valid_json() -> None:
    settings, registry, setup = _setup()
    rules = WerewolfRules()
    view = PlayerView(
        seat_no=1, role=Role.WEREWOLF, players=12, day=1, phase="night_wolf",
        candidates=[5, 6, 7, 8, 9, 10, 11, 12],
    )
    actor = RandomActor(rules.random_action)
    raw = await actor.speak(1, view, "day1.wolf.0")
    data = json.loads(raw)
    assert data["target_no"] in view.candidates


# ---------------------------------------------------------------- parse 校验（非法输出一律 JsonParseError，交给 loop 重试）

@pytest.mark.parametrize(
    "payload",
    [
        {"target_no": 9, "speech": "刀9号", "monologue": "m"},
        {"phase": "vote", "target_no": 9, "speech": "刀9号", "monologue": "m"},
        {"phase": "wolf_kill", "target_no": True, "speech": "刀1号", "monologue": "m"},
        {"phase": "wolf_kill", "target_no": 9, "speech": "刀9号", "monologue": "m", "extra": 1},
    ],
)
def test_wolf_schema_rejects_missing_wrong_or_coerced_fields(payload: dict) -> None:
    with pytest.raises(JsonParseError):
        _rules().parse(1, json.dumps(payload, ensure_ascii=False), _state(phase="night_wolf"))


@pytest.mark.parametrize(
    "payload",
    [
        {"phase": "witch_action", "monologue": "m"},
        {"phase": "witch_action", "save": False, "monologue": "m"},
        {"phase": "witch_action", "poison_target_no": None, "monologue": "m"},
        {"phase": "witch_action", "save": "false", "poison_target_no": None, "monologue": "m"},
    ],
)
def test_witch_schema_requires_explicit_strict_decisions(payload: dict) -> None:
    with pytest.raises(JsonParseError):
        _rules().parse(6, json.dumps(payload, ensure_ascii=False), _state(phase="night_witch", wolf_target=9))


def test_hunter_schema_requires_explicit_target_even_for_no_shot() -> None:
    raw = json.dumps(
        {"phase": "hunter_shot", "speech": "不开枪", "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError):
        _rules().parse(7, raw, _state(phase="hunter_shot", pending_hunter=7))


def test_shared_statement_phase_must_match_runtime_phase() -> None:
    raw = json.dumps(
        {"phase": "banter", "speech": "这不是白天发言", "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError, match="应为 discuss"):
        _rules().parse(9, raw, _state(phase="day_discuss", discuss_index=8))

def test_parse_wolf_rejects_wolf_target() -> None:
    st = _state(phase="night_wolf")
    raw = json.dumps(
        {"phase": "wolf_kill", "target_no": 2, "speech": "刀自己人", "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError):
        _rules().parse(1, raw, st)


def test_parse_seer_rejects_self() -> None:
    st = _state(phase="night_seer")
    raw = json.dumps({"phase": "seer_check", "target_no": 5, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(5, raw, st)


def test_parse_witch_rejects_save_without_knife() -> None:
    st = _state(phase="night_witch", wolf_target=None)
    raw = json.dumps(
        {"phase": "witch_action", "save": True, "poison_target_no": None, "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError):
        _rules().parse(6, raw, st)


def test_parse_witch_rejects_second_antidote() -> None:
    st = _state(phase="night_witch", wolf_target=9, witch_has_antidote=False)
    raw = json.dumps(
        {"phase": "witch_action", "save": True, "poison_target_no": None, "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError):
        _rules().parse(6, raw, st)


def test_parse_witch_rejects_save_and_poison_same_target() -> None:
    st = _state(phase="night_witch", wolf_target=9)
    raw = json.dumps(
        {"phase": "witch_action", "save": True, "poison_target_no": 9, "monologue": "m"},
        ensure_ascii=False,
    )
    with pytest.raises(JsonParseError):
        _rules().parse(6, raw, st)


def test_parse_vote_rejects_self() -> None:
    st = _state(phase="day_vote")
    raw = json.dumps({"phase": "vote", "target_no": 9, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(9, raw, st)


def test_parse_vote_rejects_dead() -> None:
    st = _kill_all(_state(phase="day_vote"), [10])
    raw = json.dumps({"phase": "vote", "target_no": 10, "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(9, raw, st)


def test_parse_hunter_allows_no_shot() -> None:
    st = _state(phase="hunter_shot", pending_hunter=7)
    raw = json.dumps(
        {"phase": "hunter_shot", "target_no": None, "speech": "不开枪", "monologue": "m"},
        ensure_ascii=False,
    )
    pr = _rules().parse(7, raw, st)
    assert json.loads(pr.action)["target_no"] is None


def test_parse_statement_rejects_empty_speech() -> None:
    st = _state(phase="day_discuss")
    raw = json.dumps({"phase": "discuss", "speech": "  ", "monologue": "m"}, ensure_ascii=False)
    with pytest.raises(JsonParseError):
        _rules().parse(9, raw, st)


# ---------------------------------------------------------------- 信息隔离（§6 不变量）

def test_wolf_chat_never_reaches_good_inbox_channels() -> None:
    st = _state(phase="night_wolf")
    raw = json.dumps(
        {"phase": "wolf_kill", "target_no": 9, "speech": "刀9号", "monologue": "m"},
        ensure_ascii=False,
    )
    pr = _rules().parse(1, raw, st)
    allowed = {2, 3, 4, "audience"}
    for d in pr.deliveries:
        assert set(d.channels) <= allowed
        assert "broadcast" not in d.channels
        for ch in d.channels:
            if isinstance(ch, int):
                assert st.seat(ch).role is Role.WEREWOLF, f"狼队夜聊进了好人 {ch}号 的频道"


def test_seer_result_only_to_seer_and_audience() -> None:
    st = _state(phase="night_seer")
    raw = json.dumps({"phase": "seer_check", "target_no": 1, "monologue": "m"}, ensure_ascii=False)
    r = _rules()
    pr = r.parse(5, raw, st)
    # 玩家自己的 sentence 不冒充裁判说出结果，只保留 audience-only 内心戏。
    assert {ch for d in pr.deliveries for ch in d.channels} == {"audience"}
    assert all("查验结果" not in d.text for d in pr.deliveries)

    # apply 后裁判以 HostEmit 回送；拓扑仍严格限制为预言家本人 + 上帝视角。
    checked = r.apply(st, pr.action)
    feedback = r.pending(checked)
    assert len(feedback) == 1
    assert json.loads(feedback[0].action)["kind"] == "seer_result"
    assert {ch for d in feedback[0].deliveries for ch in d.channels} == {5, "audience"}
    assert "1号是狼人" in feedback[0].deliveries[0].text


def test_witch_action_is_silent_to_seats() -> None:
    st = _state(phase="night_witch", wolf_target=9)
    raw = json.dumps(
        {"phase": "witch_action", "save": True, "poison_target_no": None, "monologue": "m"},
        ensure_ascii=False,
    )
    pr = _rules().parse(6, raw, st)
    channels = {ch for d in pr.deliveries for ch in d.channels}
    assert channels == {"audience"}


def test_action_prompts_only_request_phase_json() -> None:
    view = PlayerView(
        seat_no=1,
        role=Role.WEREWOLF,
        players=12,
        day=1,
        phase="night_wolf",
        candidates=[9, 10],
    )

    night = prompts.opencode_ask("night_wolf", view)
    day = prompts.opencode_ask("day_discuss", view)
    last_words = prompts.opencode_ask("last_words", view)

    assert '"phase": "wolf_kill"' in night
    assert '"phase": "discuss"' in day
    assert '"phase": "last_words"' in last_words
    for ask in (night, day, last_words):
        assert ask.endswith("只输出上述 JSON 对象，不要输出其他文字。")
        assert "自检" not in ask and "思考过程" not in ask
    assert "speech 会被一字不差地公布上桌，全场可见" not in prompts.RULES
    assert "night_wolf 的 speech 只在狼队内部传递" in prompts.RULES


def test_witch_prompt_freezes_spent_medicines_to_legal_values() -> None:
    view = _rules().view(
        6,
        _state(
            phase="night_witch",
            wolf_target=9,
            witch_has_antidote=False,
            witch_has_poison=False,
        ),
        SeatThread(),
        first_speak=True,
    )

    assert "解药已用完；save 必须为 false" in view.turn_prompt
    assert "毒药已用完；poison_target_no 必须为 null" in view.turn_prompt
    assert "你可以用解药" not in view.turn_prompt


def test_view_never_shows_others_private_intel() -> None:
    r = _rules()
    checks = [SeerCheck(target_no=1, side=Side.WEREWOLF, day=1)]
    # 白天发言：平民视角没有狼同伴名单、没有查验史，prompt 不含狼队频道信息
    st = _state(phase="day_discuss", discuss_index=8, seer_checks=checks)
    v = r.view(9, st, SeatThread(), first_speak=True)
    assert v.wolf_allies == [] and v.seer_checks == []
    assert v.witch_has_antidote is False and v.witch_has_poison is False
    assert v.night_target is None
    assert "狼队友" not in v.prompt and "查验记录" not in v.prompt
    # 夜晚狼刀：狼视角看到全部同伴（含自己以外的 3 狼），但看不到预言家查验史
    st_wolf = _state(phase="night_wolf", seer_checks=checks)
    v1 = r.view(1, st_wolf, SeatThread(), first_speak=True)
    assert sorted(v1.wolf_allies) == [2, 3, 4]
    assert v1.seer_checks == [] and "查验记录" not in v1.prompt
    # 夜晚验人：预言家视角看得到自己的查验史
    st_seer = _state(phase="night_seer", seer_checks=checks)
    v5 = r.view(5, st_seer, SeatThread(), first_speak=True)
    assert len(v5.seer_checks) == 1 and "查验记录" in v5.prompt


def test_public_situation_excludes_night_death_causes() -> None:
    """公开账本只投影死者/翻牌，不把狼刀与毒杀的裁判结算信息交给普通席位。"""
    r = _rules()
    deaths = [
        DeathRecord(seat_no=9, role=Role.VILLAGER, reason="wolf_kill", day=1),
        DeathRecord(seat_no=10, role=Role.VILLAGER, reason="poison", day=1),
    ]
    st = _kill_all(_state(phase="day_discuss", discuss_index=8, deaths=deaths), [9, 10])
    view = r.view(11, st, SeatThread(), first_speak=False)
    public = prompts.situation(view)

    assert [(d.seat_no, d.role, d.day) for d in view.deaths] == [
        (9, Role.VILLAGER, 1),
        (10, Role.VILLAGER, 1),
    ]
    assert all(not hasattr(d, "reason") for d in view.deaths)
    assert "9号" in public and "10号" in public and "翻牌：平民" in public
    assert all(secret not in public for secret in ("狼刀", "毒杀", "wolf_kill", "poison"))


def test_last_words_prompt_does_not_reveal_night_death_cause() -> None:
    death = DeathRecord(seat_no=9, role=Role.VILLAGER, reason="wolf_kill", day=1)
    st = _kill_all(_state(phase="last_words", deaths=[death], round_deaths=[death], lw_queue=[9]), [9])
    view = _rules().view(9, st, SeatThread(), first_speak=False)
    ask = prompts.opencode_ask("last_words", view)
    assert "你已经出局，现在是你的遗言时间" in ask
    assert "狼刀" not in ask and "毒杀" not in ask


def test_first_speak_restores_private_thread_without_polluting_turn_prompt() -> None:
    thread = SeatThread(
        inbox=["新的公开事件"],
        messages=[
            ThreadMessage(role="human", content="旧增量提问"),
            ThreadMessage(role="assistant", content="旧原始回答"),
        ],
    )
    view = _rules().view(9, _state(phase="day_discuss", discuss_index=8), thread, first_speak=True)

    assert "旧增量提问" in view.prompt and "旧原始回答" in view.prompt
    assert "新的公开事件" in view.turn_prompt
    assert "旧增量提问" not in view.turn_prompt and "旧原始回答" not in view.turn_prompt


def test_witch_secrets_are_fail_closed_at_view_boundary() -> None:
    r = _rules()
    st = _state(
        phase="night_witch",
        wolf_target=9,
        witch_has_antidote=False,
        witch_has_poison=True,
    )

    witch = r.view(6, st, SeatThread(), first_speak=True)
    assert witch.night_target == 9
    assert witch.witch_has_antidote is False and witch.witch_has_poison is True
    with pytest.raises(ValueError, match="无权读取"):
        r.view(11, st, SeatThread(), first_speak=True)


# ---------------------------------------------------------------- 规则推进


def test_opening_says_night_falls_exactly_once() -> None:
    opening = _rules().pending(_state())
    spoken = "\n".join(
        delivery.text
        for emit in opening
        for delivery in emit.deliveries
        if delivery.spoken
    )
    assert spoken.count("天黑请闭眼") == 1


def test_wolf_majority_emits_canonical_audience_only_result() -> None:
    r = _rules()
    st = _state(phase="night_wolf", wolf_index=0)
    for seat, target in ((1, 9), (2, 10), (3, 9), (4, 10)):
        st = r.apply(st, json.dumps({"kind": "wolf_kill", "seat": seat, "target_no": target}))
        st = r.advance(st)

    events = r.pending(st)
    assert [event.turn_id for event in events] == ["day1.wolf_kill_result"]
    event = events[0]
    assert [delivery.channels for delivery in event.deliveries] == [["audience"]]
    assert all(delivery.spoken is False for delivery in event.deliveries)
    assert json.loads(event.action) == {
        "kind": "wolf_kill_result",
        "day": 1,
        "proposals": [
            {"seat": 1, "target_no": 9},
            {"seat": 2, "target_no": 10},
            {"seat": 3, "target_no": 9},
            {"seat": 4, "target_no": 10},
        ],
        "target_no": 9,
        "resolution": "majority_then_lowest_seat",
    }
    assert r.pending(st) == events


def test_witch_double_use_emits_canonical_result_and_resolves_both_potions() -> None:
    r = _rules()
    st = _state(phase="night_witch", wolf_target=9)
    raw = json.dumps(
        {
            "phase": "witch_action",
            "save": True,
            "poison_target_no": 10,
            "monologue": "救9毒10",
        },
        ensure_ascii=False,
    )
    parsed = r.parse(6, raw, st)
    acted = r.apply(st, parsed.action)

    events = r.pending(acted)
    assert [event.turn_id for event in events] == ["day1.witch_result"]
    event = events[0]
    assert [delivery.channels for delivery in event.deliveries] == [["audience"]]
    assert all(delivery.spoken is False for delivery in event.deliveries)
    assert json.loads(event.action) == {
        "kind": "witch_result",
        "day": 1,
        "seat": 6,
        "wolf_target_no": 9,
        "save": True,
        "saved_target_no": 9,
        "poison_target_no": 10,
        "antidote_remaining": False,
        "poison_remaining": False,
    }
    assert acted.witch_has_antidote is False and acted.witch_has_poison is False

    resolved = r.advance(acted)
    assert [event.turn_id for event in r.pending(resolved)] == ["day1.dawn"]
    assert resolved.seat(9).alive and not resolved.seat(10).alive
    assert resolved.seat(10).revealed


def test_witch_may_self_save() -> None:
    r = _rules()
    st = _state(phase="night_witch", wolf_target=6)
    parsed = r.parse(
        6,
        json.dumps(
            {
                "phase": "witch_action",
                "save": True,
                "poison_target_no": None,
                "monologue": "先救自己",
            },
            ensure_ascii=False,
        ),
        st,
    )
    resolved = r.advance(r.apply(st, parsed.action))

    assert resolved.seat(6).alive
    assert all(death.seat_no != 6 for death in resolved.round_deaths)
    assert resolved.witch_has_antidote is False


def test_night_death_reveals_role() -> None:
    r = _rules()
    st = _state(phase="night_witch", wolf_target=9)
    resolved = r.advance(
        r.apply(
            st,
            json.dumps(
                {
                    "kind": "witch_action",
                    "seat": 6,
                    "save": False,
                    "poison_target_no": None,
                }
            ),
        )
    )

    assert not resolved.seat(9).alive and resolved.seat(9).revealed
    assert resolved.round_deaths[0].role is Role.VILLAGER

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


def test_knifed_and_poisoned_hunter_still_shoots() -> None:
    """同夜被刀又被毒：死因保留狼刀（先刀后毒），猎人仍可开枪。"""
    r = _rules()
    st = _state(phase="night_witch", wolf_target=7)
    st = r.apply(st, json.dumps({"kind": "witch_action", "seat": 6, "save": False, "poison_target_no": 7}))
    st = r.advance(st)
    deaths = [d for d in st.round_deaths if d.seat_no == 7]
    assert len(deaths) == 1 and deaths[0].reason == "wolf_kill"
    assert st.pending_hunter == 7 and st.phase == "hunter_shot"


def test_swapping_night_causes_does_not_change_any_public_projection() -> None:
    """刀口/毒口是裁判秘密；交换两名同角色死者的死因不得改变公开顺序。"""
    r = _rules()

    def resolve(knife: int, poison: int) -> WerewolfState:
        state = _state(phase="night_witch", wolf_target=knife)
        acted = r.apply(
            state,
            json.dumps(
                {
                    "kind": "witch_action",
                    "seat": 6,
                    "save": False,
                    "poison_target_no": poison,
                }
            ),
        )
        return r.advance(acted)

    left = resolve(9, 10)
    right = resolve(10, 9)

    assert {death.seat_no: death.reason for death in left.round_deaths} != {
        death.seat_no: death.reason for death in right.round_deaths
    }
    assert left.lw_queue == right.lw_queue == [9, 10]
    assert r.pending(left) == r.pending(right)

    left_view = r.view(9, left, SeatThread(), first_speak=False)
    right_view = r.view(9, right, SeatThread(), first_speak=False)
    assert left_view.deaths == right_view.deaths
    assert prompts.situation(left_view) == prompts.situation(right_view)


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
    assert st.votes_history[-1].top_voted == 8
    assert st.votes_history[-1].exiled is None
    kinds = [json.loads(e.action)["kind"] for e in st.host_outbox]
    assert kinds == ["vote_result", "idiot_revealed", "night_start"]
    # 播报顺序与措辞：先票型（不宣"出局"）、再翻牌免死、再天黑——不得自相矛盾
    vote_ledger, vote_result = st.host_outbox[0].deliveries
    vote_action = json.loads(st.host_outbox[0].action)
    assert vote_action["top_voted"] == 8 and vote_action["exiled"] is None
    assert vote_ledger.text.startswith("【放逐票型】") and vote_ledger.spoken is False
    assert "得票最高" in vote_result.text and "被放逐出局" not in vote_result.text
    assert "白痴牌" in st.host_outbox[1].deliveries[0].text and "免于放逐" in st.host_outbox[1].deliveries[0].text


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
    assert rec.tied and rec.top_voted is None and rec.exiled is None
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


def test_hunter_trade_that_eliminates_both_sides_awards_good_priority() -> None:
    """最后一神猎人出局后开枪带走最后一狼：双方同时达成条件，好人胜。"""
    r = _rules()
    state = _kill_all(_state(phase="hunter_shot", pending_hunter=7), [1, 2, 3, 5, 6, 7, 8])
    state = state.model_copy(
        deep=True,
        update={
            "hunter_action": {
                "kind": "hunter_shot",
                "seat": 7,
                "target_no": 4,
                "speech": "带走最后一狼",
                "monologue": "同归于尽",
            },
            "round_deaths": [
                DeathRecord(seat_no=7, role=Role.HUNTER, reason="wolf_kill", day=1)
            ],
        },
    )

    resolved = r.advance(state)

    assert resolved.winner == "good"
    assert resolved.win_reason == "all_wolves_dead"
    assert "同一次结算" in prompts.RULES and "好人胜优先" in prompts.RULES


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
    assert [event.turn_id for event in r.pending(st3)] == ["day1.wolf_kill_result"]
    # 预言家/女巫的 apply 分别产出一次裁判事件，advance 后由下一阶段 outbox 替换，绝不重发。
    checked = r.apply(
        st3,
        json.dumps({"kind": "seer_check", "seat": 5, "target_no": 1, "result": "werewolf"}),
    )
    assert [event.turn_id for event in r.pending(checked)] == ["day1.seer_result"]
    st3 = r.advance(checked)
    medicated = r.apply(
        st3,
        json.dumps({"kind": "witch_action", "seat": 6, "save": False, "poison_target_no": None}),
    )
    assert [event.turn_id for event in r.pending(medicated)] == ["day1.witch_result"]
    st3 = r.advance(medicated)
    dawn = r.pending(st3)
    assert [e.turn_id for e in dawn] == ["day1.dawn"]
    assert json.loads(dawn[0].action)["deaths"][0]["seat_no"] == 9
