"""通用主持人事件发布器。"""

from __future__ import annotations

from core.events import emit
from core.threads import SeatThread, broadcast


class Host:
    """把主持人口播同时写进玩家线程和 god-view 事件流。"""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def announce(self, threads: dict[int, SeatThread], beat: str, text: str, *, quest: int, attempt: int) -> None:
        base = f"{quest}.{attempt}.host.{beat}"
        n = self._seen[base] = self._seen.get(base, 0) + 1
        broadcast(threads, text)
        emit("host", beat=beat, call_id=base if n == 1 else f"{base}.{n}", text=text)
