"""LLMPlayer._act 的鲁棒性：对 LLM 这层外部不可控依赖，"不符预期"要退避重试，而非一次失败
就让整局炸。覆盖两类"不符预期"：

1. 调用本身抛错（API 失败/超时的代理）——重试后成功，整局照常收官。
2. 抽取出的结构化结果字段非法——这里是"该有发言却空了"（NonEmptyStr 触发），同样重试救回。

全程 --player random：零 key、零网络。退避 sleep 在测试里打成无操作，避免真等。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from avalon import player as player_mod
from avalon.run import run_game
from avalon.settings import AvalonSettings


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """把退避等待打成无操作——重试逻辑照跑，只是不真睡，测试秒回。"""

    async def _instant(_delay):
        return None

    monkeypatch.setattr(player_mod.asyncio, "sleep", _instant)


async def test_transient_call_failure_is_retried(tmp_path, monkeypatch) -> None:
    """前两次模型调用注入 RuntimeError，_act 退避重试后成功——整局仍干净收官。"""
    run_dir = tmp_path / "g"
    orig_call = player_mod.FakeBackend.call
    state = {"fails_left": 2}

    async def flaky(self, phase, view, spec, messages):
        if state["fails_left"] > 0:
            state["fails_left"] -= 1
            raise RuntimeError("transient boom")
        return await orig_call(self, phase, view, spec, messages)

    monkeypatch.setattr(player_mod.FakeBackend, "call", flaky)

    final = await run_game(AvalonSettings(player="random", players=5, seed=7, replay=run_dir))

    assert state["fails_left"] == 0  # 两次失败都真的被注入了
    assert final.finished is True and final.winner in {"good", "evil"}

    # 失败尝试落了盘（ok=false），证明重试不丢审计；同 call_id 因重试可出现 >2 条。
    llm = [json.loads(ln) for ln in (run_dir / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(c["ok"] is False for c in llm)


async def test_empty_speech_triggers_retry(tmp_path, monkeypatch) -> None:
    """抽取器首次吐出"发言为空"的 JSON（NonEmptyStr 拒绝 → JsonParseError），_act 重跑救回。

    这正是线上那次 6 号空发言的根因：模型只产内心戏、漏了发言。修复后它不再静默上桌。"""
    run_dir = tmp_path / "g"
    orig_call = player_mod.FakeBackend.call
    state = {"injected": False}

    async def flaky(self, phase, view, spec, messages):
        if phase == "discuss.extract" and not state["injected"]:
            state["injected"] = True
            return AIMessage(content='{"speech": "", "monologue": "只有内心戏，发言空了"}')
        return await orig_call(self, phase, view, spec, messages)

    monkeypatch.setattr(player_mod.FakeBackend, "call", flaky)

    final = await run_game(AvalonSettings(player="random", players=5, seed=7, replay=run_dir))

    assert state["injected"] is True  # 注入确实发生
    assert final.finished is True

    # 空发言那次抽取被记为失败（字段校验失败），且没有任何上桌发言是空的。
    llm = [json.loads(ln) for ln in (run_dir / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(c["ok"] is False and "校验" in (c["error"] or "") for c in llm)

    events = [json.loads(ln) for ln in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    speeches = [e["text"] for e in events if e["type"] == "discuss"]
    assert speeches and all(s.strip() for s in speeches)  # 上桌发言无一为空
