"""deliver 的路由隔离（设计文档 §6 不变量）。

信息隔离压在纯代码拓扑上：int → 指定席位 inbox；broadcast → 全员；audience → 不进
任何席位（只由 journal 的观众流承载）。deliver 是只按 channels 写的机械函数，物理上
无法把私密话写进非授权 inbox——这组测试把这条不变量钉死。
"""

from __future__ import annotations

from core.sr.loop import deliver
from core.sr.types import Delivery
from core.threads import SeatThread


def _threads(n: int = 3) -> dict[int, SeatThread]:
    return {no: SeatThread() for no in range(1, n + 1)}


def test_int_channel_writes_only_that_seat():
    threads = _threads()
    deliver([Delivery(text="私聊", channels=[2])], threads)
    assert threads[1].inbox == []
    assert threads[2].inbox == ["私聊"]
    assert threads[3].inbox == []


def test_broadcast_writes_every_seat():
    threads = _threads()
    deliver([Delivery(text="公开", channels=["broadcast"])], threads)
    for t in threads.values():
        assert t.inbox == ["公开"]


def test_audience_never_enters_any_inbox():
    """内心独白/观众事件：任何席位的 inbox 都拿不到。"""
    threads = _threads()
    deliver([Delivery(text="给观众的话", channels=["audience"])], threads)
    for t in threads.values():
        assert t.inbox == []


def test_mixed_channels_route_independently():
    """沉默动作形态（自己 + 观众）：自己听见自己，其他席位一无所知。"""
    threads = _threads()
    deliver([Delivery(text="自救了一下", channels=[1, "audience"])], threads)
    assert threads[1].inbox == ["自救了一下"]
    assert threads[2].inbox == []
    assert threads[3].inbox == []


def test_empty_text_is_dropped():
    threads = _threads()
    deliver([Delivery(text="", channels=["broadcast"])], threads)
    for t in threads.values():
        assert t.inbox == []
