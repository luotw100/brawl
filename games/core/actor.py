"""通用 LLM actor 运行时。"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import perf_counter
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from core.events import decision_id
from core.models import ModelSpec
from core.parsing import JsonParseError, parse_json
from core.recorder import Recorder

_ROLE = {SystemMessage: "system", HumanMessage: "human", AIMessage: "assistant"}


class ModelBackend(Protocol):
    """一次模型调用的抽象。"""

    async def call(self, phase: str, view: Any, spec: ModelSpec, messages: list[BaseMessage]) -> BaseMessage: ...


class LiveBackend:
    """OpenAI 兼容真模型后端。"""

    def __init__(self) -> None:
        self._models: dict[str, ChatOpenAI] = {}

    def _model(self, spec: ModelSpec) -> ChatOpenAI:
        model = self._models.get(spec.name)
        if model is None:
            model = ChatOpenAI(
                model=spec.model_id,
                base_url=spec.base_url,
                api_key=os.environ[spec.api_key_env],
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                timeout=spec.timeout,
                extra_body=spec.extra_body or None,
            )
            self._models[spec.name] = model
        return model

    async def call(self, phase: str, view: Any, spec: ModelSpec, messages: list[BaseMessage]) -> BaseMessage:
        return await self._model(spec).ainvoke(messages)


_MAX_ATTEMPTS = 5
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0


class LLMActor:
    """玩家模型自由文本对话 + 固定轻量 extractor 解析校验。"""

    def __init__(self, recorder: Recorder, extractor: ModelSpec, backend: ModelBackend) -> None:
        self._recorder = recorder
        self._extractor_spec = extractor
        self._backend = backend

    async def _invoke(self, phase: str, view: Any, spec: ModelSpec, messages: list[BaseMessage]) -> str:
        t0 = perf_counter()
        try:
            resp = await self._backend.call(phase, view, spec, messages)
        except Exception as e:
            self._record(phase, view, spec, messages, None, None, round((perf_counter() - t0) * 1000), f"API 调用失败：{e}")
            raise
        latency_ms = round((perf_counter() - t0) * 1000)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        self._record(phase, view, spec, messages, text, resp, latency_ms, None)
        return text

    async def _converse(self, phase: str, view: Any, system_text: str) -> str:
        spec = view.model
        history = [HumanMessage(m.content) if m.role == "human" else AIMessage(m.content) for m in view.messages]
        messages: list[BaseMessage] = [SystemMessage(system_text), *history, HumanMessage(view.prompt)]
        return await self._invoke(phase, view, spec, messages)

    async def _extract[T: BaseModel](self, phase: str, view: Any, extract_system: str, instruction: str, model_cls: type[T]) -> T:
        messages: list[BaseMessage] = [SystemMessage(extract_system), HumanMessage(instruction)]
        text = await self._invoke(f"{phase}.extract", view, self._extractor_spec, messages)
        try:
            return parse_json(text, model_cls)
        except JsonParseError as e:
            self._record(f"{phase}.extract", view, self._extractor_spec, messages, text, None, 0, str(e))
            raise

    async def _act[R](self, phase: str, view: Any, once: Callable[[], Awaitable[R]]) -> R:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return await once()
            except Exception as e:
                if attempt == _MAX_ATTEMPTS:
                    raise
                delay = min(_BACKOFF_BASE * 2 ** (attempt - 1), _BACKOFF_CAP) + random.random()
                print(
                    f"[retry] 座位{view.seat_no} {phase} 第 {attempt}/{_MAX_ATTEMPTS} 次失败，"
                    f"{delay:.1f}s 后重试：{e}",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable: 循环要么 return 要么在末次 raise")

    def _record(
        self,
        phase: str,
        view: Any,
        spec: ModelSpec,
        messages: list[BaseMessage],
        text: str | None,
        resp: BaseMessage | None,
        latency_ms: int,
        error: str | None,
    ) -> None:
        usage = dict(resp.usage_metadata) if (resp is not None and getattr(resp, "usage_metadata", None)) else {}
        self._recorder.record_llm_call(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "call_id": decision_id(view.quest_no, view.attempt, phase, view.seat_no),
                "quest": view.quest_no,
                "attempt": view.attempt,
                "phase": phase,
                "seat_no": view.seat_no,
                "model": spec.model_id,
                "messages": [{"role": _ROLE.get(type(m), "?"), "content": m.content} for m in messages],
                "output": text,
                "usage": usage,
                "latency_ms": latency_ms,
                "ok": error is None,
                "error": error,
            }
        )

