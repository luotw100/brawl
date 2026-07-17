"""OpenCode headless server 后端与协议。

通过 HTTP API 与 `opencode serve` 通信：每个座位一个 session，保留该座位记忆。
调用方渲染好最终 prompt 文本后交给本后端，由它发送并轮询 assistant 回复。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, Protocol

import httpx

from core.models import ModelSpec
from core.opencode_runtime import OpenCodeConnection
from core.sr.messages import ChatMessage


class ModelBackend(Protocol):
    """一次 agent 调用的抽象。"""

    async def call(self, phase: str, view: Any, spec: ModelSpec, messages: list[ChatMessage]) -> ChatMessage: ...


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
        connection: OpenCodeConnection,
        default_model: str = "",
        seat_models: dict[int, str] | None = None,
        timeout: float = 180.0,
        poll_interval: float = 0.5,
        max_poll_time: float = 300.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=connection.base_url.rstrip("/"),
            auth=(connection.username, connection.password),
            timeout=timeout,
            trust_env=False,
        )
        self._workspace_root = connection.workspace_root.rstrip("/")
        self._default_model = default_model
        self._seat_models = seat_models or {}
        self._poll_interval = poll_interval
        self._max_poll_time = max_poll_time
        self._sessions: dict[int, str] = {}
        self._session_locks: dict[int, asyncio.Lock] = {}
        self._last_user: dict[int, str | None] = {}

    @staticmethod
    def _messages_to_text(messages: list[ChatMessage]) -> str:
        """把消息列表序列化成一段带角色标记的文本。"""
        if len(messages) == 1 and messages[0].role == "human":
            return messages[0].content
        tag = {"system": "system", "human": "user", "assistant": "assistant"}
        return "\n\n".join(f"[{tag.get(m.role, m.role)}]\n{m.content}" for m in messages)

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

    async def _request(self, method: str, path: str, *, directory: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        headers["x-opencode-directory"] = directory
        kwargs["headers"] = headers
        resp = await self._client.request(method, path, **kwargs)
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()

    async def list_models(self) -> list[dict[str, Any]]:
        """查询 OpenCode server 当前可用的模型列表。"""
        data = await self._request("GET", "/config/providers", directory=self._workspace_root)
        providers = data.get("providers", []) if isinstance(data, dict) else []
        models: list[dict[str, Any]] = []
        for provider in providers:
            raw = provider.get("models", {}) if isinstance(provider, dict) else {}
            if isinstance(raw, dict):
                models.extend(m for m in raw.values() if isinstance(m, dict))
        return models

    async def _create_session(self, seat_no: int) -> str:
        payload: dict[str, Any] = {
            "title": f"game-seat-{seat_no}",
            "permission": [{"permission": "*", "pattern": "*", "action": "deny"}],
        }
        data = await self._request(
            "POST",
            "/session",
            directory=self._directory_for_seat(seat_no),
            json=payload,
        )
        return data["id"]

    def _directory_for_seat(self, seat_no: int) -> str:
        return f"{self._workspace_root}/seat-{seat_no:02d}"

    def _lock_for_seat(self, seat_no: int) -> asyncio.Lock:
        if seat_no not in self._session_locks:
            self._session_locks[seat_no] = asyncio.Lock()
        return self._session_locks[seat_no]

    async def _ensure_session(self, seat_no: int) -> str:
        async with self._lock_for_seat(seat_no):
            if seat_no not in self._sessions:
                self._sessions[seat_no] = await self._create_session(seat_no)
            return self._sessions[seat_no]

    async def _send_prompt(self, session_id: str, seat_no: int, text: str, model_ref: str) -> str:
        """发送 prompt，返回新建 user message 的 id。"""
        directory = self._directory_for_seat(seat_no)
        existing_users = self._user_ids(
            await self._request("GET", f"/session/{session_id}/message", directory=directory)
        )
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if model_ref:
            payload["model"] = self._parse_ref(model_ref)
        await self._request(
            "POST",
            f"/session/{session_id}/prompt_async",
            directory=directory,
            json=payload,
        )

        # prompt_async 只返回 204，需要轮询找到新建的 user message
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            data = await self._request("GET", f"/session/{session_id}/message", directory=directory)
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
            directory = self._directory_for_seat(seat_no)
            # 先终止可能还在跑的老请求
            await self._request("POST", f"/session/{session_id}/abort", directory=directory)
            data = await self._request("GET", f"/session/{session_id}/message", directory=directory)
            for msg in self._message_items(data):
                info = self._message_info(msg)
                if info.get("role") == "assistant" and info.get("parentID") == user_id:
                    try:
                        await self._request(
                            "DELETE",
                            f"/session/{session_id}/message/{info['id']}",
                            directory=directory,
                        )
                    except Exception as e:
                        print(
                            f"[warn] 删除 OpenCode assistant message {info['id']} 失败（忽略）：{e}",
                            file=sys.stderr,
                        )
            try:
                await self._request(
                    "DELETE",
                    f"/session/{session_id}/message/{user_id}",
                    directory=directory,
                )
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

            # 少数模型会把最终 JSON 放进 reasoning part，却不生成 text part。
            # 只接受完整的 JSON 对象，避免把普通推理过程当作玩家输出。
            for part in reversed(parts):
                if not isinstance(part, dict) or part.get("type") != "reasoning":
                    continue
                candidate = part.get("text")
                if not isinstance(candidate, str):
                    continue
                candidate = candidate.strip()
                try:
                    value = json.loads(candidate)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(value, dict):
                    return candidate
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

    async def _wait_for_response(self, session_id: str, seat_no: int, user_id: str) -> tuple[str, str]:
        """等待 user_id 对应的 assistant 回复，返回 (assistant_id, text)。"""
        deadline = time.monotonic() + self._max_poll_time
        directory = self._directory_for_seat(seat_no)

        while time.monotonic() < deadline:
            data = await self._request("GET", f"/session/{session_id}/message", directory=directory)
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

    async def call(self, phase: str, view: Any, spec: ModelSpec, messages: list[ChatMessage]) -> ChatMessage:
        seat_no = getattr(view, "seat_no", 0)
        session_id = await self._ensure_session(seat_no)
        text = self._messages_to_text(messages)
        model_ref = self._model_for_seat(seat_no)
        # 清理上一轮标记的失败消息（parse 失败 rollback 过的 / 上次调用崩在中途的）
        await self._cleanup_failed_turn(session_id, seat_no)
        user_id = await self._send_prompt(session_id, seat_no, text, model_ref)
        self._last_user[seat_no] = user_id
        # 调用成功后 _last_user 保持标记：调用方（loop）parse 过才 commit_turn 摘除，
        # parse 不过会 rollback 删掉这轮——失败轮绝不留在 session 里污染下次上下文。
        assistant_id, response_text = await self._wait_for_response(session_id, seat_no, user_id)
        return ChatMessage(role="assistant", content=response_text)

    def commit_turn(self, seat_no: int) -> None:
        """本轮产出已被调用方验收（parse 通过）：摘除失败标记，session 保留这轮对话。"""
        self._last_user[seat_no] = None

    async def rollback(self, seat_no: int) -> None:
        """本轮产出让调用方拒收（parse 失败）：立即从 session 删掉这轮的 user+assistant 消息。"""
        session_id = self._sessions.get(seat_no)
        if session_id is not None:
            await self._cleanup_failed_turn(session_id, seat_no)

    async def close(self) -> None:
        await self._client.aclose()
