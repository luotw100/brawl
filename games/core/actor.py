"""通用 agent actor 运行时。

现在 games 只抽象「agent 后端」：
- OpenCodeBackend：通过 HTTP 与 `opencode serve` 通信
- RandomAgent：本地模拟 agent 返回（零网络，用于测试/离线跑局）

不再保留裸 LLM API 调用链路。
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import perf_counter
from typing import Any, Protocol

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from core.events import decision_id
from core.models import ModelSpec
from core.recorder import Recorder

_ROLE = {SystemMessage: "system", HumanMessage: "human", AIMessage: "assistant"}


class ModelBackend(Protocol):
    """一次 agent 调用的抽象。"""

    async def call(self, phase: str, view: Any, spec: ModelSpec, messages: list[BaseMessage]) -> BaseMessage: ...


_MAX_ATTEMPTS = 5
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0


class OpenCodeBackend:
    """OpenCode headless server 后端。

    通过 HTTP API 与 `opencode serve` 通信：
    - 每个座位一个 session，保留该座位记忆
    - 每次决策发送调用方渲染好的一段文本；是否全量 seed 或只发增量由玩法层决定
    - 轮询 /session/{id}/message 直到拿到完整 assistant 回复

    这里不依赖任何自定义 agent 配置：把 OpenCode 当作普通 agent 接口用。

    模型选择优先级（单座位）：
        1. 该座位在 `seat_models` 中指定的 providerID/modelID
        2. `default_model`（CLI --opencode-model）
        3. 让 OpenCode server 使用其默认模型
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        username: str = "opencode",
        password: str | None = None,
        default_model: str = "",
        seat_models: dict[int, str] | None = None,
        timeout: float = 180.0,
        poll_interval: float = 0.5,
        max_poll_time: float = 300.0,
    ) -> None:
        password = password or os.environ.get("OPENCODE_SERVER_PASSWORD", "")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(username, password) if password else None,
            timeout=timeout,
        )
        self._default_model = default_model
        self._seat_models = seat_models or {}
        self._poll_interval = poll_interval
        self._max_poll_time = max_poll_time
        self._sessions: dict[int, str] = {}
        self._session_locks: dict[int, asyncio.Lock] = {}
        self._last_user: dict[int, str | None] = {}

    @staticmethod
    def _messages_to_text(messages: list[BaseMessage]) -> str:
        """把 LangChain 消息序列化成一段带角色标记的文本。"""
        if len(messages) == 1 and isinstance(messages[0], HumanMessage):
            return str(messages[0].content)
        parts: list[str] = []
        for m in messages:
            if isinstance(m, SystemMessage):
                parts.append(f"[system]\n{m.content}")
            elif isinstance(m, HumanMessage):
                parts.append(f"[user]\n{m.content}")
            elif isinstance(m, AIMessage):
                parts.append(f"[assistant]\n{m.content}")
            else:
                parts.append(f"[{m.type}]\n{str(m.content)}")
        return "\n\n".join(parts)

    @staticmethod
    def _parse_ref(ref: str) -> dict[str, str]:
        """把 providerID/modelID 字符串解析成 OpenCode ModelRef 对象。"""
        provider_id, sep, model_id = ref.partition("/")
        if not sep:
            raise ValueError(
                f"OpenCode model 必须是 providerID/modelID 格式,例如 opencode-go/deepseek-v4-pro,收到: {ref!r}"
            )
        return {"providerID": provider_id, "modelID": model_id}

    def _model_for_seat(self, seat_no: int) -> str:
        """返回指定座位应使用的模型引用（providerID/modelID），可能为空。"""
        return self._seat_models.get(seat_no, self._default_model)

    def has_session(self, seat_no: int) -> bool:
        """这个后端实例里是否已经为该座位创建过 OpenCode session。"""
        return seat_no in self._sessions

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._client.request(method, path, **kwargs)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()

    async def list_models(self) -> list[dict[str, Any]]:
        """查询 OpenCode server 当前可用的模型列表。"""
        data = await self._request("GET", "/config/providers")
        providers = data.get("providers", []) if isinstance(data, dict) else []
        models: list[dict[str, Any]] = []
        for provider in providers:
            raw = provider.get("models", {}) if isinstance(provider, dict) else {}
            if isinstance(raw, dict):
                models.extend(m for m in raw.values() if isinstance(m, dict))
        return models

    async def _create_session(self, seat_no: int) -> str:
        payload: dict[str, Any] = {"title": f"game-seat-{seat_no}"}
        data = await self._request("POST", "/session", json=payload)
        return data["id"]

    def _lock_for_seat(self, seat_no: int) -> asyncio.Lock:
        if seat_no not in self._session_locks:
            self._session_locks[seat_no] = asyncio.Lock()
        return self._session_locks[seat_no]

    async def _ensure_session(self, seat_no: int) -> str:
        async with self._lock_for_seat(seat_no):
            if seat_no not in self._sessions:
                self._sessions[seat_no] = await self._create_session(seat_no)
            return self._sessions[seat_no]

    async def _send_prompt(self, session_id: str, text: str, model_ref: str) -> str:
        """发送 prompt，返回新建 user message 的 id。"""
        existing_users = self._user_ids(
            await self._request("GET", f"/session/{session_id}/message")
        )
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if model_ref:
            payload["model"] = self._parse_ref(model_ref)
        await self._request("POST", f"/session/{session_id}/prompt_async", json=payload)

        # prompt_async 只返回 204，需要轮询找到新建的 user message
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            data = await self._request("GET", f"/session/{session_id}/message")
            for msg in self._message_items(data):
                info = self._message_info(msg)
                if info.get("role") == "user":
                    msg_id = info.get("id")
                    if msg_id is not None and str(msg_id) not in existing_users:
                        # 确认文本匹配，避免拿到别人并发的消息
                        parts = msg.get("parts", [])
                        msg_text = " ".join(
                            p.get("text", "") for p in parts if p.get("type") == "text"
                        )
                        if msg_text.strip() == text.strip():
                            return str(msg_id)
            await asyncio.sleep(0.05)

        raise RuntimeError(f"在 OpenCode session {session_id} 中未找到新建的 user message")

    async def _cleanup_failed_turn(self, session_id: str, seat_no: int) -> None:
        """清理上一轮失败的 user + assistant 消息，避免失败上下文累积。"""
        user_id = self._last_user.get(seat_no)
        if not user_id:
            return

        try:
            # 先终止可能还在跑的老请求
            await self._request("POST", f"/session/{session_id}/abort")
            data = await self._request("GET", f"/session/{session_id}/message")
            for msg in self._message_items(data):
                info = self._message_info(msg)
                if info.get("role") == "assistant" and info.get("parentID") == user_id:
                    try:
                        await self._request(
                            "DELETE", f"/session/{session_id}/message/{info['id']}"
                        )
                    except Exception as e:
                        print(
                            f"[warn] 删除 OpenCode assistant message {info['id']} 失败（忽略）：{e}",
                            file=sys.stderr,
                        )
            try:
                await self._request("DELETE", f"/session/{session_id}/message/{user_id}")
            except Exception as e:
                print(
                    f"[warn] 删除 OpenCode user message {user_id} 失败（忽略）：{e}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"[warn] 清理 OpenCode session {session_id} 失败轮失败（忽略）：{e}",
                file=sys.stderr,
            )
        finally:
            self._last_user[seat_no] = None

    def _extract_text(self, msg: dict[str, Any]) -> str:
        """从 assistant message 中抽取文本内容。"""
        parts = msg.get("parts", [])
        if isinstance(parts, list):
            texts = [part.get("text", "") for part in parts if part.get("type") == "text"]
            text = "\n".join(texts).strip()
            if text:
                return text
        content = msg.get("content", [])
        if isinstance(content, list):
            texts = [part.get("text", "") for part in content if part.get("type") == "text"]
            return "\n".join(texts).strip()
        if isinstance(content, str):
            return content.strip()
        return ""

    @staticmethod
    def _message_items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [msg for msg in data if isinstance(msg, dict)]
        if not isinstance(data, dict):
            return []
        wrapped = data.get("data")
        if isinstance(wrapped, list):
            return [msg for msg in wrapped if isinstance(msg, dict)]
        if "info" in data or "role" in data or "type" in data:
            return [data]
        return []

    @staticmethod
    def _message_info(msg: dict[str, Any]) -> dict[str, Any]:
        info = msg.get("info")
        return info if isinstance(info, dict) else msg

    @staticmethod
    def _user_ids(data: Any) -> set[str]:
        """取当前消息列表中所有 user 消息的 id 集合。"""
        ids: set[str] = set()
        for msg in OpenCodeBackend._message_items(data):
            info = OpenCodeBackend._message_info(msg)
            if info.get("role") == "user":
                msg_id = info.get("id")
                if msg_id is not None:
                    ids.add(str(msg_id))
        return ids

    async def _wait_for_response(self, session_id: str, user_id: str) -> tuple[str, str]:
        """等待 user_id 对应的 assistant 回复，返回 (assistant_id, text)。"""
        deadline = time.monotonic() + self._max_poll_time

        while time.monotonic() < deadline:
            data = await self._request("GET", f"/session/{session_id}/message")
            messages = self._message_items(data)
            for msg in messages:
                info = self._message_info(msg)
                role = info.get("role", msg.get("type"))
                if role != "assistant":
                    continue
                if info.get("parentID") != user_id:
                    continue
                error = info.get("error") or msg.get("error")
                if error:
                    raise RuntimeError(f"OpenCode agent error: {error}")
                finish = info.get("finish", msg.get("finish"))
                completed = info.get("time", {}).get("completed")
                if not (finish or completed):
                    continue
                text = self._extract_text(msg)
                if text:
                    return str(info.get("id")), text
            await asyncio.sleep(self._poll_interval)

        raise TimeoutError(f"等待 OpenCode session {session_id} 响应超时")

    async def call(self, phase: str, view: Any, spec: ModelSpec, messages: list[BaseMessage]) -> AIMessage:
        seat_no = getattr(view, "seat_no", 0)
        session_id = await self._ensure_session(seat_no)
        text = self._messages_to_text(messages)
        model_ref = self._model_for_seat(seat_no)
        # 清理上一轮失败的残留消息，避免失败上下文累积
        await self._cleanup_failed_turn(session_id, seat_no)
        user_id = await self._send_prompt(session_id, text, model_ref)
        self._last_user[seat_no] = user_id
        try:
            assistant_id, response_text = await self._wait_for_response(session_id, user_id)
            self._last_user[seat_no] = None
            return AIMessage(content=response_text)
        except Exception:
            # 保留 _last_user，下次 call 会清理失败轮
            raise

    async def close(self) -> None:
        await self._client.aclose()


class AgentActor:
    """通用 agent 玩家：负责调用后端、重试、落 llm_calls 审计。

    子类只需实现 `_say(phase, view)`：把 system + history + prompt 拼成消息列表，
    调 `self._invoke(...)` 拿到后端返回的原始文本，再按玩法约定解析成结构化决策。
    """

    def __init__(self, recorder: Recorder, backend: ModelBackend) -> None:
        self._recorder = recorder
        self._backend = backend

    async def _invoke(self, phase: str, view: Any, spec: ModelSpec | None, messages: list[BaseMessage]) -> str:
        t0 = perf_counter()
        try:
            resp = await self._backend.call(phase, view, spec, messages)
        except Exception as e:
            self._record(phase, view, spec, messages, None, None, round((perf_counter() - t0) * 1000), f"agent 调用失败：{e}")
            raise
        latency_ms = round((perf_counter() - t0) * 1000)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        self._record(phase, view, spec, messages, text, resp, latency_ms, None)
        return text

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
        spec: ModelSpec | None,
        messages: list[BaseMessage],
        text: str | None,
        resp: BaseMessage | None,
        latency_ms: int,
        error: str | None,
    ) -> None:
        usage = dict(resp.usage_metadata) if (resp is not None and getattr(resp, "usage_metadata", None)) else {}
        model_name = "unknown"
        if spec is not None:
            model_name = spec.opencode_model or spec.name
        self._recorder.record_llm_call(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "call_id": decision_id(view.quest_no, view.attempt, phase, view.seat_no),
                "quest": view.quest_no,
                "attempt": view.attempt,
                "phase": phase,
                "seat_no": view.seat_no,
                "model": model_name,
                "messages": [{"role": _ROLE.get(type(m), "?"), "content": m.content} for m in messages],
                "output": text,
                "usage": usage,
                "latency_ms": latency_ms,
                "ok": error is None,
                "error": error,
            }
        )

    async def close(self) -> None:
        if hasattr(self._backend, "close"):
            try:
                await self._backend.close()
            except Exception as e:
                print(f"[warn] 关闭 agent 后端失败（忽略）：{e}", file=sys.stderr)


# 保留旧名，避免外部引用瞬间全部失效；后续可逐步迁到 AgentActor。
LLMActor = AgentActor
