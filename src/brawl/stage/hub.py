"""WS 集线器：所有连上的页面收到同一条指令流——"频道"语义的落点。"""

from __future__ import annotations

import asyncio

from fastapi import WebSocket
from pydantic import BaseModel


class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, msg: BaseModel) -> None:
        data = msg.model_dump_json()
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(data)
            except Exception:
                await self.leave(ws)  # 死连接顺手清掉，下次广播不再碰它
