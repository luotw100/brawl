"""deliver 的路由隔离（设计文档 §6 不变量）。

信息隔离压在纯代码拓扑上：int → 指定席位 inbox；broadcast → 全员；audience → 不进
任何席位（只由 journal 的观众流承载）。deliver 是只按 channels 写的机械函数，物理上
无法把私密话写进非授权 inbox——这组测试把这条不变量钉死。
"""

from __future__ import annotations

import pytest

from brawl.runtime.loop import _deliver_validated, _validated_deliveries
from brawl.runtime.threads import SeatThread
from brawl.runtime.types import Delivery


def _deliver(
    deliveries: list[Delivery],
    threads: dict[int, SeatThread],
    speaker: int | str = "host",
) -> None:
    """公开 deliver 删除后，测试直取校验 + 路由两段内部件（语义与原公开函数一致）。"""
    _deliver_validated(_validated_deliveries(deliveries, threads), threads, speaker)


def _threads(n: int = 3) -> dict[int, SeatThread]:
    return {no: SeatThread() for no in range(1, n + 1)}


def test_int_channel_writes_only_that_seat():
    threads = _threads()
    _deliver([Delivery(text="私聊", channels=[2])], threads)
    assert threads[1].inbox == []
    assert threads[2].inbox == ["私聊"]
    assert threads[3].inbox == []


def test_broadcast_writes_every_seat():
    threads = _threads()
    _deliver([Delivery(text="公开", channels=["broadcast"])], threads)
    for t in threads.values():
        assert t.inbox == ["公开"]


def test_audience_never_enters_any_inbox():
    """内心独白/观众事件：任何席位的 inbox 都拿不到。"""
    threads = _threads()
    _deliver([Delivery(text="给观众的话", channels=["audience"])], threads)
    for t in threads.values():
        assert t.inbox == []


def test_mixed_channels_route_independently():
    """沉默动作形态（自己 + 观众）：自己听见自己，其他席位一无所知。"""
    threads = _threads()
    _deliver([Delivery(text="自救了一下", channels=[1, "audience"])], threads)
    assert threads[1].inbox == ["自救了一下"]
    assert threads[2].inbox == []
    assert threads[3].inbox == []


def test_empty_text_is_dropped():
    threads = _threads()
    _deliver([Delivery(text="", channels=["broadcast"])], threads)
    for t in threads.values():
        assert t.inbox == []


def test_unknown_private_seat_fails_before_any_delivery():
    """整组路由先预检；后面的坏 seat 不能让前面的广播产生半批副作用。"""
    threads = _threads()
    deliveries = [
        Delivery(text="不应投递", channels=["broadcast"]),
        Delivery(text="坏路由", channels=[99]),
    ]

    with pytest.raises(RuntimeError, match="未知座位"):
        _deliver(deliveries, threads)

    assert all(thread.inbox == [] for thread in threads.values())


# ---------------------------------------------------------------- 归属渲染
#
# Delivery.text 是裸内容；写 inbox 的那一刻才按 speaker 包归属。归属只服务 LLM inbox，
# TTS/字幕拿裸文本（见 tts._utterances / stagecast）。


def test_speech_gets_attributed_on_entry_to_inbox():
    """kind="speech"：裸发言包成 `N号：「…」` 入箱。"""
    threads = _threads()
    _deliver([Delivery(text="我过", channels=["broadcast"], kind="speech")], threads, speaker=2)
    for t in threads.values():
        assert t.inbox == ["2号：「我过」"]


def test_narration_enters_inbox_verbatim():
    """kind="narration"（默认）：完整成句原样入箱，不包归属。"""
    threads = _threads()
    _deliver([Delivery(text="队长 2号 提议的队伍：1号、2号。", channels=["broadcast"])], threads, speaker=2)
    for t in threads.values():
        assert t.inbox == ["队长 2号 提议的队伍：1号、2号。"]


def test_speech_attribution_overrides_default_label():
    """attribution 覆盖默认标签（狼聊的"狼队 N号（提议刀X号）"式扩展归属）。"""
    threads = _threads()
    _deliver(
        [
            Delivery(
                text="刀 3 号",
                channels=[2],
                kind="speech",
                attribution="狼队 1号（提议刀3号）",
            )
        ],
        threads,
        speaker=1,
    )
    assert threads[2].inbox == ["狼队 1号（提议刀3号）：「刀 3 号」"]


def test_host_deliveries_never_get_attributed():
    """host 句子即使是 speech kind 也不包归属（实践中 host 只产 narration）。"""
    threads = _threads()
    _deliver([Delivery(text="天黑请闭眼。", channels=["broadcast"], kind="speech")], threads, speaker="host")
    for t in threads.values():
        assert t.inbox == ["天黑请闭眼。"]
