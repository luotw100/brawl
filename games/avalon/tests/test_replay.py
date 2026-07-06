"""回放层的两条守则：

- 生成路径零 wall-clock：Renderer 缺省用 InstantTypist（整段瞬出、绝不 sleep），
  打字机只在回放显式注入 PacedTypist。这守住"games 是快照生成器、不在生成器里计时"。
- events ↔ llm_calls 严格对齐：每个事件背后的决策 call_id 都能在 llm_calls 里命中
  主调用与 extract 抽取两格（decision_id 同一公式两端各自算）。回放据此旁注成本。

asyncio_mode=auto：async 测试直接跑。
"""

from __future__ import annotations

import json

from avalon.console import InstantTypist, Renderer
from avalon.replay import ReplaySettings, _event_calls, _index_calls, run_replay
from avalon.run import run_game
from avalon.settings import AvalonSettings


def test_renderer_defaults_to_instant_typist() -> None:
    """生成路径用的就是裸 Renderer()：缺省 typist 必须是瞬出、不计时的 InstantTypist。"""
    assert isinstance(Renderer().typist, InstantTypist)


async def test_every_event_call_id_resolves_to_main_call(tmp_path) -> None:
    """一局落盘后，每个事件的每个 call_id 都在 llm_calls 里命中主调用。

    random 后端走完整 AgentPlayer 链路、每次决策必有一条记录且 ok，故对齐是 1↔1。"""
    run_dir = tmp_path / "g"
    await run_game(AvalonSettings(player="random", players=10, percival=True, morgana=True, mordred=True, oberon=True, lady=True, seed=7, replay=run_dir))

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    calls = [json.loads(line) for line in (run_dir / "llm_calls.jsonl").read_text().splitlines()]
    idx = _index_calls(calls)

    seen = 0
    for ev in events:
        for label, cid in _event_calls(ev):
            assert cid is not None, f"{ev['type']} 的 {label} 缺 call_id"
            assert cid in idx, f"{cid} 未在 llm_calls 命中"
            assert "main" in idx[cid], f"{cid} 无主调用：{idx[cid].keys()}"
            seen += 1
    assert seen > 0
    # 反向：每条主调用也都被某事件认领（无孤儿）——回放成本旁注无遗漏。
    main_ids = {c.get("call_id") for c in calls if not str(c["phase"]).endswith(".extract")}
    claimed = {cid for ev in events for _, cid in _event_calls(ev)}
    assert main_ids <= claimed


async def test_run_replay_pace_zero_with_cost(tmp_path, capsys) -> None:
    """回放驱动跑通：pace=0 瞬出 + show_cost 旁注 + 末尾总账，不抛错、有输出。"""
    run_dir = tmp_path / "g"
    await run_game(AvalonSettings(player="random", players=5, seed=3, replay=run_dir))

    run_replay(ReplaySettings.load([str(run_dir), "--pace", "0", "--show-cost"]))
    out = capsys.readouterr().out
    assert "开局" in out and "调用量" in out and "主调用 · 分模型" in out and "抽取调用 · 分模型" in out
