"""overlay 画布：悬浮在演出之上的内容片清单（导播台挂/撤，ttl 到期自动撤）。

游戏无关。任何变更调注入的 publish（app 层广播完整舞台状态）。将来的"礼物机器人/
平台对接"就是一个自动调 put/remove 的内部 worker，这层抽象不动。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from server.protocol import OverlayItem


class OverlayBoard:
    def __init__(self, publish: Callable[[], Awaitable[None]]) -> None:
        self._publish = publish
        self._items: dict[str, OverlayItem] = {}
        self._reapers: dict[str, asyncio.Task] = {}

    def items(self) -> list[OverlayItem]:
        return list(self._items.values())

    async def put(self, item: OverlayItem) -> OverlayItem:
        if not item.id:
            item = item.model_copy(update={"id": uuid.uuid4().hex[:8]})
        self._cancel_reaper(item.id)
        self._items[item.id] = item
        if item.ttl_seconds is not None:
            self._reapers[item.id] = asyncio.create_task(self._reap(item.id, item.ttl_seconds))
        await self._publish()
        return item

    async def remove(self, item_id: str) -> None:
        self._cancel_reaper(item_id)
        self._items.pop(item_id, None)
        await self._publish()

    async def _reap(self, item_id: str, ttl: float) -> None:
        await asyncio.sleep(ttl)
        self._reapers.pop(item_id, None)
        self._items.pop(item_id, None)
        await self._publish()

    def _cancel_reaper(self, item_id: str) -> None:
        task = self._reapers.pop(item_id, None)
        if task is not None:
            task.cancel()
