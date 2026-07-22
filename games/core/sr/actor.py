"""新 loop 的 actor 适配层。

把现有 OpenCode 后端封装成 core.sr.loop.Actor 协议，规则层的 view 对象只负责
拼出 .prompt；actor 负责把 prompt 发进 opencode/session 并返回
原始文本。审计真相由 OpenCode server 自己持有，不在 runtime 重复造一份即刻丢弃的字典。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from core.sr.errors import RetryableActorError
from core.sr.messages import ChatMessage
from core.sr.opencode_backend import ModelBackend


class _ViewProxy:
    """OpenCodeBackend 需要从 view 上读取 seat_no，规则 view 不一定带这个字段。

    用代理对象补 seat_no，避免修改规则层返回的 view（它可能是 Pydantic / dataclass
    等不可变对象）。
    """

    __slots__ = ("_view", "seat_no")

    def __init__(self, view: Any, seat_no: int) -> None:
        self._view = view
        self.seat_no = seat_no

    def __getattr__(self, name: str) -> Any:
        return getattr(self._view, name)


class OpenCodeActor:
    """把 OpenCodeBackend / ModelBackend 包装成 core.sr.loop.Actor。"""

    def __init__(self, backend: ModelBackend) -> None:
        self._backend = backend

    async def speak(self, seat: int, view: Any, turn_id: str) -> str:
        prompt = getattr(view, "prompt", None)
        if not isinstance(prompt, str):
            raise TypeError(f"view.prompt must be str, got {type(prompt)}")

        messages = [ChatMessage(role="human", content=prompt)]
        proxy = _ViewProxy(view, seat_no=seat)
        try:
            resp = await self._backend.call(turn_id, proxy, messages)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (408, 429) or status >= 500:
                raise RetryableActorError(f"OpenCode HTTP 暂时失败：{status}") from exc
            raise
        except (httpx.TransportError, TimeoutError) as exc:
            raise RetryableActorError(f"OpenCode 暂时不可用：{exc}") from exc
        return resp.content

    def commit_turn(self, seat: int) -> None:
        """本轮产出已被 loop 验收（parse 通过）：转告 backend 摘除失败标记。"""
        self._backend.commit_turn(seat)

    async def rollback(self, seat: int) -> None:
        """本轮产出被 loop 拒收（parse 失败）：转告 backend 从 session 删掉这轮消息。"""
        await self._backend.rollback(seat)

class RandomActor:
    """零网络 actor：由规则层传入一个生成器，按 view 产出合法 raw。

    生成器签名：generator(seat: int, view: Any) -> str。规则层最清楚当前 phase 要输出
    什么 JSON，所以 sampler 下沉到 rules；core 只负责调用。
    """

    def __init__(self, generator: Callable[[int, Any], str]) -> None:
        self._generator = generator

    async def speak(self, seat: int, view: Any, turn_id: str) -> str:
        return self._generator(seat, view)

    def commit_turn(self, seat: int) -> None: ...  # 无会话可清理，空操作

    async def rollback(self, seat: int) -> None: ...

__all__ = ["OpenCodeActor", "RandomActor"]
