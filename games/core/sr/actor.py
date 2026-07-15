"""新 loop 的 actor 适配层。

把现有 OpenCode 后端封装成 core.sr.loop.Actor 协议，规则层的 view 对象只负责
拼出 .prompt 与可选的 .spec；actor 负责把 prompt 发进 opencode/session 并返回
审计行。seq/turn_id 由 loop/journal 补，actor 不再关心 quest/attempt。
"""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from core.models import ModelSpec
from core.sr.opencode_backend import ModelBackend


@runtime_checkable
class PromptView(Protocol):
    """OpenCodeActor 期望的规则层 view 对象最小契约。"""

    prompt: str


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

    async def speak(self, seat: int, view: Any, turn_id: str) -> tuple[str, dict[str, Any] | None]:
        prompt = getattr(view, "prompt", None)
        if not isinstance(prompt, str):
            raise TypeError(f"view.prompt must be str, got {type(prompt)}")

        messages: list[BaseMessage] = [HumanMessage(content=prompt)]
        spec = getattr(view, "spec", None)
        if spec is not None and not isinstance(spec, ModelSpec):
            raise TypeError(f"view.spec must be ModelSpec or None, got {type(spec)}")

        proxy = _ViewProxy(view, seat_no=seat)
        t0 = perf_counter()
        resp = await self._backend.call(turn_id, proxy, spec, messages)
        latency_ms = round((perf_counter() - t0) * 1000)

        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        usage = dict(resp.usage_metadata) if getattr(resp, "usage_metadata", None) else {}
        model_name = _model_name(spec)

        audit = {
            "ts": _now_iso(),
            "call_id": turn_id,
            "seat_no": seat,
            "model": model_name,
            "messages": [{"role": _ROLE.get(type(m), "?"), "content": m.content} for m in messages],
            "output": text,
            "usage": usage,
            "latency_ms": latency_ms,
            "ok": True,
            "error": None,
        }
        return text, audit

    async def close(self) -> None:
        if hasattr(self._backend, "close"):
            await self._backend.close()  # type: ignore[no-any-return]


class RandomActor:
    """零网络 actor：由规则层传入一个生成器，按 view 产出合法 raw。

    生成器签名：generator(seat: int, view: Any) -> str。规则层最清楚当前 phase 要输出
    什么 JSON，所以 sampler 下沉到 rules；core 只负责调用并落审计行。
    """

    def __init__(self, generator: Callable[[int, Any], str]) -> None:
        self._generator = generator

    async def speak(self, seat: int, view: Any, turn_id: str) -> tuple[str, dict[str, Any] | None]:
        raw = self._generator(seat, view)
        return raw, {
            "call_id": turn_id,
            "seat_no": seat,
            "model": "random",
            "output": raw,
            "ok": True,
        }

    async def close(self) -> None: ...


def _model_name(spec: ModelSpec | None) -> str:
    if spec is None:
        return "unknown"
    return spec.opencode_model or spec.name or "unknown"


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


_ROLE = {HumanMessage: "human", AIMessage: "assistant"}

__all__ = ["OpenCodeActor", "RandomActor", "PromptView"]
