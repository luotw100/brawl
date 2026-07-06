"""端到端冒烟 + 跨数据通道的不变量：用 --player random 真跑完整一局（零 key、零网络）。

守三条核心不变量：
- 必终局：组队最多连否 5 次、任务最多 5 个、刺杀只有一次，随机后端下一局必然分出胜负，
  且 winner / win_reason 组合自洽（蓝方只能赢在"刺杀落空"）。
- 私密通道：他人可见的通道（ProposalRecord、各席位线程的 human 消息）永不携带角色/
  情报/独白/任务暗牌——谁出的破坏票只进 god-view 事件流；湖中仙女的真实查验结果只进
  持有者自己的线程。
- replay 契约：--replay 落盘三件套（events.jsonl / llm_calls.jsonl / meta.json）。

asyncio_mode=auto（见 pyproject），async 测试函数直接由 pytest-asyncio 跑。
"""

from __future__ import annotations

import json

from avalon import rules
from avalon.run import run_game
from avalon.settings import AvalonSettings
from avalon.state import GameState, ProposalRecord


def _assert_outcome_consistent(final) -> None:
    assert final.finished is True
    assert final.winner in {"good", "evil"}
    assert final.win_reason in {"quests", "rejections", "assassination"}
    if final.winner == "good":
        # 蓝方唯一的赢法：拿满 3 个任务后挺过刺杀
        assert final.win_reason == "assassination"
    if final.win_reason == "quests":
        assert final.quests_failed >= rules.QUESTS_TO_WIN
    if final.win_reason == "rejections":
        assert final.rejections >= rules.MAX_REJECTIONS
    if final.win_reason == "assassination":
        assert final.quests_succeeded >= rules.QUESTS_TO_WIN


def _thread_text(final: GameState, no: int) -> str:
    """某席位整局看到+说过的全部文本（已发消息 + 终局仍未 flush 的 inbox）。"""
    t = final.threads[no]
    return "\n".join([m.content for m in t.messages] + list(t.inbox))


async def test_game_finishes_with_consistent_outcome() -> None:
    """随机后端多种子各跑一局：必定终局，胜负与终局原因自洽。"""
    for seed in range(8):
        final = await run_game(AvalonSettings(player="random", players=5, seed=seed))
        _assert_outcome_consistent(final)


async def test_larger_table_with_double_fail_rule_runs() -> None:
    """7 人局（触发"第 4 任务需 2 张破坏票"的人数档）也能正常跑完并给出合法胜负。"""
    final = await run_game(AvalonSettings(player="random", players=7, seed=1))
    _assert_outcome_consistent(final)


async def test_full_lineup_ten_player_game_runs() -> None:
    """10 人标准全角色局（梅林+派西维尔+4忠臣 / 刺客+莫甘娜+莫德雷德+奥伯伦）跑通且胜负自洽。"""
    for seed in range(4):
        final = await run_game(
            AvalonSettings(player="random", players=10, percival=True, morgana=True, mordred=True, oberon=True, seed=seed)
        )
        _assert_outcome_consistent(final)
        assert sorted(s.role.value for s in final.seats) == sorted(
            ["merlin", "percival", "servant", "servant", "servant", "servant", "assassin", "morgana", "mordred", "oberon"]
        )


async def test_lady_of_the_lake_module_runs() -> None:
    """7 人 + 湖中仙女：跑通且令牌轨迹合法——历任持有者互不重复、每次传递都向全员广播过
    一次当众宣布（以任一席位线程里的主持词「仙女环节」计数）。"""
    for seed in range(6):
        final = await run_game(AvalonSettings(player="random", players=7, lady=True, seed=seed))
        _assert_outcome_consistent(final)
        assert final.lady_holder is not None
        assert len(final.lady_used) == len(set(final.lady_used))  # 不能重复持有
        announcements = _thread_text(final, 1).count("仙女环节")
        assert announcements == len(final.lady_used) - 1  # 每次传递对应一次当众宣布


async def test_lady_disabled_by_default() -> None:
    """不开 --lady：状态里令牌全程为空，不触发查验。"""
    final = await run_game(AvalonSettings(player="random", players=7, seed=1))
    assert final.lady_holder is None and final.lady_used == []
    assert "仙女环节" not in _thread_text(final, 1)


async def test_threads_never_leak_secrets() -> None:
    """铁律：席位线程的 human 通道（裁判灌入侧）不携带秘密——
    他人的内心戏/任务暗牌的当事人绝不出现（离线后端的独白与暗牌话术是唯一来源，
    它们只该出现在各自席位的 assistant 原文里）；湖中仙女的真实查验结果只进历任持有者的线程。
    ProposalRecord 的 schema 也装不下秘密字段。"""
    assert {"role", "side", "intel", "monologue", "cards"}.isdisjoint(ProposalRecord.model_fields)

    final = await run_game(AvalonSettings(player="random", players=7, lady=True, seed=3))
    for no, t in final.threads.items():
        human = "\n".join(m.content for m in t.messages if m.role == "human")
        assert "牌已扣下" not in human  # 任务暗牌的内心戏（只在出牌者自己的 assistant 原文里）
        assert "抛硬币定的" not in human  # 表决内心戏
        assert "闭眼点的将" not in human  # 组队内心戏
        if "真实阵营是" in human:  # 湖中仙女私密结果只可能出现在历任持有者的线程
            assert no in final.lady_used


async def test_threads_are_alternating_and_complete() -> None:
    """线程形状：每席位 human/assistant 严格交替成对（append-only 一问一答），
    且每个席位整局至少行动过一次（阿瓦隆人人都要表决）。"""
    final = await run_game(AvalonSettings(player="random", players=5, seed=2))
    for t in final.threads.values():
        assert t.messages, "每个席位至少行动过一次"
        assert len(t.messages) % 2 == 0
        assert all(m.role == ("human" if i % 2 == 0 else "assistant") for i, m in enumerate(t.messages))


async def test_history_is_public_ledger_of_all_proposals() -> None:
    """history 是全部提案的明牌账本：被否决的 approved=False 且无任务结果；
    上场的 approved=True 且结果完整；表决人数始终为全员（没人出局）。"""
    final = await run_game(AvalonSettings(player="random", players=5, seed=3))
    for rec in final.history:
        assert len(rec.votes) == 5 and set(rec.votes) == {1, 2, 3, 4, 5}
        if rec.approved:
            assert rec.result in {"success", "fail"} and rec.fails is not None
        else:
            assert rec.result is None and rec.fails is None


async def test_replay_writes_three_files(tmp_path) -> None:
    """--replay 落一局完整快照：三件套齐全，events 非空，meta 含确定性开局方案。"""
    run_dir = tmp_path / "game1"
    cast = ["shade", "fox", "diva", "shy", "honest"]
    await run_game(AvalonSettings(player="random", players=5, seed=4, replay=run_dir, cast=cast))

    events = (run_dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert events, "events.jsonl 应记录整局事件时间线"
    types = {json.loads(line)["type"] for line in events}
    assert {"game_start", "deal", "propose", "vote_result", "game_end"} <= types

    # 离线后端也走完整 AgentPlayer 链路，每次决策的一条 agent call 都落盘
    # （这正是把假后端下沉到模型调用层、让调试路径贴近生产的收益）。
    calls = (run_dir / "llm_calls.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert calls, "llm_calls.jsonl 应逐条记录每次 agent 调用（含离线后端）"
    records = [json.loads(line) for line in calls]
    assert all(r.keys() >= {"phase", "seat_no", "messages", "output", "ok"} for r in records)
    phases = {r["phase"] for r in records}
    assert "propose" in phases  # 决策阶段被走到
    assert all(r["ok"] for r in records)  # 假后端产出永远合法、解析不该失败

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert set(meta["setup"]) == {
        "cast",
        "host_cast",
        "names",
        "host_name",
        "voices",
        "host_voice",
        "self_hosted_voices",
        "host_self_hosted_voice",
        "voice_designs",
        "host_voice_design",
        "strategies",
        "models",
        "roles",
        "first_leader",
        "lady_holder",
    }
    assert meta["setup"]["voice_designs"] and meta["setup"]["host_voice_design"]
    assert meta["setup"]["self_hosted_voices"] and meta["setup"]["host_self_hosted_voice"] is not None
