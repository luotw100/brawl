"""断点续生成的端到端不变量：跑到一半注入崩溃 → --resume 续跑 → 产物干净收官。

守的不变量（即"水位截断 + 从对应 checkpoint 续跑"是否严格对齐）：
- 崩溃前已落盘的前缀逐行不变（续跑不回改历史）。
- 续跑后整局收官（最后一条是 host/closing），且胜负自洽。
- 无重复：events 里带 call_id 的行各自唯一；llm_calls 里每个 call_id 至多两条
  （一次决策 = converse + 同 id 的 .extract，正好两条；若断点节点被重跑且没截干净，
  会冒出第三、四条）。

注入的"崩溃"必须是 BaseException（这里用 _HardCrash）而非普通 Exception——后者会被
LLMPlayer._act 当作"LLM 抽风"退避重试掉，根本不会传播；resume 兜的是进程级硬崩（OOM/被 kill/
断电），正是 retry 故意不接的那一类。用 BaseException 既忠实模拟硬崩、又保持单次不重试，
让"每个 call_id ≤ 2 条"的不变量成立。

全程 --player random：零 key、零网络（FakeBackend 注入崩溃）。
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from avalon import player as player_mod
from avalon import resume as resume_mod
from avalon.run import run_game
from avalon.settings import AvalonSettings


class _HardCrash(BaseException):
    """模拟进程级硬崩：BaseException，绕过 _act 的 retry（它只接 Exception）直接传播。"""


async def test_resume_after_crash_completes_clean(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "g"
    orig_call = player_mod.FakeBackend.call
    state = {"n": 0, "boom_at": 40, "armed": True}

    async def flaky(self, phase, view, spec, messages):
        if state["armed"]:
            state["n"] += 1
            if state["n"] == state["boom_at"]:
                raise _HardCrash("injected crash")
        return await orig_call(self, phase, view, spec, messages)

    monkeypatch.setattr(player_mod.FakeBackend, "call", flaky)

    # 1) 新局跑到第 40 次模型调用时崩
    with pytest.raises(_HardCrash, match="injected crash"):
        await run_game(AvalonSettings(player="random", players=5, seed=11, replay=run_dir))

    # 崩溃后：有可续水位，且盘上可能残留断点节点写了一半的脏行（events 行数 ≥ 水位）
    wm = resume_mod.last_watermark(run_dir)
    assert wm is not None
    prefix = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()[: wm.events_n]
    assert (run_dir / "events.jsonl").exists()

    # 2) 续跑（不再注入崩溃）
    state["armed"] = False
    final = await run_game(AvalonSettings(player="random", players=5, seed=11, replay=run_dir, resume=True))

    # 收官且自洽
    assert final.finished is True
    assert final.winner in {"good", "evil"}

    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    # 前缀逐行不变：续跑只在水位之后续写
    assert lines[: wm.events_n] == prefix
    events = [json.loads(ln) for ln in lines]
    assert events[-1]["type"] == "host" and events[-1]["beat"] == "closing"

    # events 的 call_id 唯一（带 call_id 的那些）
    cids = [e["call_id"] for e in events if e.get("call_id")]
    assert len(cids) == len(set(cids)), "events 出现重复 call_id（断点节点没截干净/重复续写）"

    # llm_calls 每个 call_id 至多两条（converse + .extract）
    llm = [json.loads(ln) for ln in (run_dir / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    counts = Counter(c["call_id"] for c in llm)
    assert all(v <= 2 for v in counts.values()), f"llm_calls 出现 >2 条同 call_id：{[k for k, v in counts.items() if v > 2]}"


async def test_resume_requires_existing_dir_with_checkpoint(tmp_path) -> None:
    """--resume 指向一个没有 checkpoint 的目录要在边界报错，而不是默默重开。"""
    from avalon.settings import SettingsError
    empty = tmp_path / "nope"
    empty.mkdir()
    with pytest.raises(SettingsError):
        await run_game(AvalonSettings(player="random", players=5, seed=1, replay=empty, resume=True))
