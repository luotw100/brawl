"""loop 的 speak+parse 同单元重试语义。

actor 调用失败（网络/超时）与 rules.parse 校验不过（坏 JSON、非法目标）同等对待：
都重新调 actor 拿新产出；重试上限后 fail-fast 抛出，绝不把非法产出写进日志。
重试间隔用 monkeypatch 打掉的 _sleep，测试零等待。
"""

from __future__ import annotations

from typing import Any

import pytest

import core.sr.loop as loop_mod
from core.sr.dummy import SEATS, DummyRules
from core.sr.journal import NullJournal
from core.sr.loop import run
from core.threads import SeatThread


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(delay: float) -> None:
        return None

    monkeypatch.setattr(loop_mod, "_sleep", _instant)


class FlakySpeakActor:
    """前 fail_times 次 speak 抛异常，之后正常。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def speak(self, seat: int, view: Any, turn_id: str) -> tuple[str, None]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("模拟网络超时")
        return f"{seat}号说：大家好", None

    async def close(self) -> None: ...


class FlakyParseActor:
    """前 fail_times 次返回非法产出（DummyRules.parse 只接受非空文本，空串抛 ValueError）。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def speak(self, seat: int, view: Any, turn_id: str) -> tuple[str, None]:
        self.calls += 1
        if self.calls <= self.fail_times:
            return "", None  # 空产出：语义校验不过
        return f"{seat}号说：大家好", None

    async def close(self) -> None: ...


class StrictDummyRules(DummyRules):
    """空产出视为非法（模拟玩法 parse 硬校验）。"""

    def parse(self, seat: int, raw: str, state: dict):
        if not raw.strip():
            raise ValueError("空产出非法")
        return super().parse(seat, raw, state)


def _threads() -> dict[int, SeatThread]:
    return {no: SeatThread() for no in SEATS}


@pytest.mark.asyncio
async def test_flaky_speak_recovers_within_retries(tmp_path):
    actor = FlakySpeakActor(fail_times=2)
    actors = {no: (actor if no == SEATS[0] else FlakySpeakActor(0)) for no in SEATS}
    final = await run(DummyRules(), setup=None, actors=actors, journal=NullJournal(), threads=_threads())
    assert final["step"] == 3
    assert actor.calls == 3  # 2 次失败 + 第 3 次成功


@pytest.mark.asyncio
async def test_parse_failure_triggers_respeak(tmp_path):
    actor = FlakyParseActor(fail_times=2)
    actors = {no: (actor if no == SEATS[0] else FlakyParseActor(0)) for no in SEATS}
    final = await run(StrictDummyRules(), setup=None, actors=actors, journal=NullJournal(), threads=_threads())
    assert final["step"] == 3
    assert actor.calls == 3  # parse 失败也算一次失败调用，重拿一份新产出


@pytest.mark.asyncio
async def test_retry_exhaustion_fails_fast(tmp_path):
    actor = FlakySpeakActor(fail_times=loop_mod._MAX_RETRIES)  # 每次都失败
    actors = {no: (actor if no == SEATS[0] else FlakySpeakActor(0)) for no in SEATS}
    with pytest.raises(TimeoutError):
        await run(DummyRules(), setup=None, actors=actors, journal=NullJournal(), threads=_threads())
    assert actor.calls == loop_mod._MAX_RETRIES  # 恰好试满上限，不多调


@pytest.mark.asyncio
async def test_parse_exhaustion_fails_fast(tmp_path):
    actor = FlakyParseActor(fail_times=loop_mod._MAX_RETRIES)
    actors = {no: (actor if no == SEATS[0] else FlakyParseActor(0)) for no in SEATS}
    with pytest.raises(ValueError, match="空产出非法"):
        await run(StrictDummyRules(), setup=None, actors=actors, journal=NullJournal(), threads=_threads())
    assert actor.calls == loop_mod._MAX_RETRIES
