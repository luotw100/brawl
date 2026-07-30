"""OpenCode headless server 后端与协议。

通过 HTTP API 与 ``opencode serve`` 通信：每个座位一个持久 session。后端同时支持
两条协议：

* legacy：玩法层可提供 ``view.schema``，请求使用 OpenCode StructuredOutput；为了
  兼容已经存在的玩法，这条路径仍只信同步 POST 返回的单个 assistant id，不读取会被
  OpenCode 1.17.20 的 format 读侧 bug 弄坏的消息列表。
* agent/tool v2：``view.agent`` 为非空字符串时启用。创建 session 后先用
  ``view.seed_prompt`` + ``noReply`` 写入一次私有前缀；每次决策显式指定 agent、
  model 和唯一允许的动作工具，不带 format。同步 POST 完成后再 GET 完整消息列表，
  从本轮所有 assistant 中提取恰好一次成功工具调用和与它同消息的自然语言。

两条路径都把未被规则层 commit 的 user/assistant id 精确记账。任何无法完整枚举的
v2 回合、拿不到同步响应的回合或删除失败都会毒化 session，禁止在未知上下文上重试。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from brawl.runtime.errors import FatalActorError, RetryableActorError
from brawl.runtime.messages import ChatMessage
from brawl.runtime.opencode_runtime import OpenCodeConnection, OpenCodeRuntimeError


class ModelBackend(Protocol):
    """一次 agent 调用的抽象。"""

    async def call(self, phase: str, view: Any, messages: list[ChatMessage]) -> ChatMessage: ...
    def commit_turn(self, seat_no: int) -> None: ...
    async def rollback(self, seat_no: int) -> None: ...
    async def close(self) -> None: ...


class OpenCodeSessionPoisoned(FatalActorError):
    """OpenCode session 无法证明干净（存在无法枚举/删除的残留消息）。"""


@dataclass(frozen=True)
class _PendingTurn:
    """一次尚未由规则层验收的 OpenCode 回合。

    legacy 路径只有同步响应里的一个 assistant；v2 路径保存 GET 枚举出的本轮全部
    assistant。回滚必须逆序删除所有子消息，再删除 user 父消息。
    """

    session_id: str
    user_id: str
    assistant_ids: tuple[str, ...]


class OpenCodeBackend:
    """OpenCode headless server 后端。

    通过 HTTP API 与 `opencode serve` 通信：
    - 每个座位一个 session，保留该座位记忆
    - 每次决策发送调用方渲染好的一段文本；是否全量 seed 或只发增量由玩法层决定
    - legacy 调用可继续使用 StructuredOutput
    - agent/tool v2 调用使用玩法层选择的受信 agent 与动作工具

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
        generation_timeout: float = 600.0,
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
        self._generation_timeout = generation_timeout
        self._sessions: dict[int, str] = {}
        self._session_agent_modes: dict[int, bool] = {}
        self._seeded_sessions: set[str] = set()
        self._session_locks: dict[int, asyncio.Lock] = {}
        self._pending_turns: dict[int, _PendingTurn] = {}
        self._poisoned_sessions: dict[int, str] = {}

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

    async def list_model_refs(self) -> set[str]:
        """查询 OpenCode server 当前可用模型的 providerID/modelID 全集。"""
        data = await self._request("GET", "/config/providers", directory=self._workspace_root)
        providers = data.get("providers", []) if isinstance(data, dict) else []
        refs: set[str] = set()
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id")
            raw = provider.get("models", {})
            if isinstance(provider_id, str) and isinstance(raw, dict):
                refs.update(f"{provider_id}/{model_id}" for model_id in raw)
        return refs

    async def assert_models_available(self, refs: Iterable[str]) -> None:
        """开工前一次性核对：引用的模型必须在 server 可用列表里（空引用 = server 默认，跳过）。

        模型名打错的典型事故不该推迟到该座位第一次发言才炸（整局开局作废）；
        这里是世界一致性校验，违规抛 OpenCodeRuntimeError，由 CLI 友好打印。
        """
        wanted = {ref for ref in refs if ref}
        if not wanted:
            return
        available = await self.list_model_refs()
        missing = sorted(wanted - available)
        if missing:
            raise OpenCodeRuntimeError(
                f"OpenCode server 不提供这些模型引用：{missing}；"
                f"请检查 models.yaml 的 opencode_model（server 可用：{sorted(available)}）"
            )

    async def _create_session(self, seat_no: int, *, agent_mode: bool) -> str:
        permissions = [
            {"permission": "*", "pattern": "*", "action": "deny"},
        ]
        if not agent_mode:
            # legacy format 路径需要显式放行 OpenCode 内置的 StructuredOutput；
            # agent/tool 路径完全不需要也不应拥有这项权限。
            permissions.append(
                {
                    "permission": "StructuredOutput",
                    "pattern": "*",
                    "action": "allow",
                }
            )
        payload: dict[str, Any] = {
            "title": f"game-seat-{seat_no}",
            "permission": permissions,
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

    async def _ensure_session(self, seat_no: int, *, agent_mode: bool) -> str:
        async with self._lock_for_seat(seat_no):
            if seat_no not in self._sessions:
                self._sessions[seat_no] = await self._create_session(
                    seat_no,
                    agent_mode=agent_mode,
                )
                self._session_agent_modes[seat_no] = agent_mode
            elif (
                seat_no in self._session_agent_modes
                and self._session_agent_modes[seat_no] is not agent_mode
            ):
                raise FatalActorError(
                    f"OpenCode seat {seat_no} 已按 "
                    f"{'agent/tool' if self._session_agent_modes[seat_no] else 'legacy'} "
                    "协议创建 session，不能在同一 session 切换协议"
                )
            return self._sessions[seat_no]

    async def _send_turn(
        self,
        session_id: str,
        seat_no: int,
        text: str,
        model_ref: str,
        schema: dict[str, Any] | None,
        *,
        agent: str | None = None,
        tools: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """同步发送一轮对话，返回完整响应（含 info 与 parts）。

        拿不到完整响应时（超时/传输错误/取消），服务端可能已创建本回合消息，
        也可能仍在后台生成；无论 legacy 还是 v2 都无法在这个时点证明 session
        干净，因此尽力 abort 后毒化（见模块 docstring）。
        """
        directory = self._directory_for_seat(seat_no)
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if model_ref:
            payload["model"] = self._parse_ref(model_ref)
        if agent is not None:
            payload["agent"] = agent
        if tools is not None:
            payload["tools"] = tools
        if schema is not None:
            payload["format"] = {"type": "json_schema", "schema": schema}

        try:
            resp = await self._request(
                "POST",
                f"/session/{session_id}/message",
                directory=directory,
                json=payload,
                timeout=httpx.Timeout(self._generation_timeout, connect=30.0),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                # 4xx 由 HTTP 校验层在回合处理前拒出，服务端没有创建本回合消息，
                # session 仍然干净：408/429 由 actor 映射为可重试，其余 4xx 为 fatal。
                raise
            # 5xx 可能是回合处理中途的崩溃：状态未知，按拿不到响应处理。
            poisoned = self._poison(seat_no, session_id, exc)
            await self._abort_best_effort(session_id, seat_no)
            raise poisoned from exc
        except BaseException as exc:
            poisoned = self._poison(seat_no, session_id, exc)
            await self._abort_best_effort(session_id, seat_no)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise poisoned from exc

        info = resp.get("info") if isinstance(resp, dict) else None
        if not isinstance(info, dict):
            raise self._poison(
                seat_no,
                session_id,
                RuntimeError(f"OpenCode 同步响应缺少 assistant info：{str(resp)[:200]}"),
            )
        user_id = info.get("parentID")
        assistant_id = info.get("id")
        if not (isinstance(user_id, str) and user_id and isinstance(assistant_id, str) and assistant_id):
            # 响应里没有可记账的消息 id，本回合无法精确回滚 → 视为状态未知。
            raise self._poison(
                seat_no,
                session_id,
                RuntimeError(f"OpenCode 同步响应缺少消息 id：{str(info)[:200]}"),
            )
        return resp

    async def _abort_best_effort(self, session_id: str, seat_no: int) -> None:
        """尽力终止服务端可能还在跑的生成；结果不影响已注定的毒化。"""

        async def abort() -> None:
            with contextlib.suppress(Exception):
                await self._request(
                    "POST",
                    f"/session/{session_id}/abort",
                    directory=self._directory_for_seat(seat_no),
                )

        task = asyncio.create_task(abort())
        # 清理不能被调用方取消到一半；取消由调用方自己的异常路径传播。
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)

    async def _cleanup_failed_turn(self, seat_no: int) -> None:
        """事务性回滚失败轮：按本地记账的 id 删除未验收的 user+assistant 消息。"""
        self._raise_if_poisoned(seat_no)
        pending = self._pending_turns.get(seat_no)
        if pending is None:
            return
        if pending.session_id != self._sessions.get(seat_no):
            raise self._poison(
                seat_no,
                pending.session_id,
                RuntimeError(f"pending turn 属于 session {pending.session_id}，当前却是 {self._sessions.get(seat_no)}"),
            )

        cleanup = asyncio.create_task(self._delete_turn(seat_no, pending))
        cancelled: asyncio.CancelledError | None = None
        try:
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    # 清理不能被调用方取消到一半；记住取消，确认事务结束后再传播。
                    cancelled = exc
        except BaseException as exc:
            raise self._poison(seat_no, pending.session_id, exc) from exc

        try:
            cleanup.result()
        except BaseException as exc:
            raise self._poison(seat_no, pending.session_id, exc) from exc

        # 删除已精确完成，至此才允许清 marker。
        if self._pending_turns.get(seat_no) == pending:
            del self._pending_turns[seat_no]
        if cancelled is not None:
            raise cancelled

    async def _delete_turn(self, seat_no: int, pending: _PendingTurn) -> None:
        """逆序删所有 assistant（子），再删 user（父）。"""
        session_id = pending.session_id
        directory = self._directory_for_seat(seat_no)
        for assistant_id in reversed(pending.assistant_ids):
            await self._request(
                "DELETE",
                f"/session/{session_id}/message/{assistant_id}",
                directory=directory,
            )
        await self._request(
            "DELETE",
            f"/session/{session_id}/message/{pending.user_id}",
            directory=directory,
        )

    def _raise_if_poisoned(self, seat_no: int) -> None:
        reason = self._poisoned_sessions.get(seat_no)
        if reason is not None:
            session_id = self._sessions.get(seat_no, "unknown")
            raise OpenCodeSessionPoisoned(
                f"OpenCode seat {seat_no} session {session_id} 已被毒化，禁止原地重试：{reason}"
            )

    def _poison(self, seat_no: int, session_id: str, cause: BaseException) -> OpenCodeSessionPoisoned:
        reason = f"{type(cause).__name__}: {cause}"
        self._poisoned_sessions[seat_no] = reason
        return OpenCodeSessionPoisoned(
            f"OpenCode seat {seat_no} session {session_id} 无法确认干净"
            f"（无法枚举、校验或删除完整回合），禁止原地重试：{reason}"
        )

    def _extract_text(self, msg: dict[str, Any]) -> str:
        """从同步响应的 assistant 消息中抽取文本内容（无 format 的纯文本路径）。"""
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

    async def _send_and_register(
        self,
        session_id: str,
        seat_no: int,
        text: str,
        model_ref: str,
        schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """legacy 发送并登记单个 assistant：parse 过才 commit，否则精确 rollback。"""
        resp = await self._send_turn(session_id, seat_no, text, model_ref, schema)
        info = resp["info"]
        self._pending_turns[seat_no] = _PendingTurn(
            session_id=session_id,
            user_id=info["parentID"],
            assistant_ids=(info["id"],),
        )
        return resp

    @staticmethod
    def _v2_text(msg: dict[str, Any]) -> str:
        """只取 assistant 的显式 text part，不把 reasoning 当玩家发言。"""
        parts = msg.get("parts")
        if not isinstance(parts, list):
            return ""
        texts = [
            part["text"]
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        return "\n".join(texts).strip()

    async def _seed_v2(
        self,
        session_id: str,
        seat_no: int,
        *,
        seed_prompt: str,
        agent: str,
        model_ref: str,
    ) -> None:
        """用 noReply 写入一次私有前缀；任何失败都意味着 session 状态未知。"""
        if session_id in self._seeded_sessions:
            return
        payload: dict[str, Any] = {
            "parts": [{"type": "text", "text": seed_prompt}],
            "noReply": True,
            "agent": agent,
            "model": self._parse_ref(model_ref),
            "tools": {"*": False},
        }
        try:
            resp = await self._request(
                "POST",
                f"/session/{session_id}/message",
                directory=self._directory_for_seat(seat_no),
                json=payload,
                timeout=httpx.Timeout(self._generation_timeout, connect=30.0),
            )
        except BaseException as exc:
            poisoned = self._poison(seat_no, session_id, exc)
            await self._abort_best_effort(session_id, seat_no)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise poisoned from exc

        info = resp.get("info") if isinstance(resp, dict) else None
        if not (
            isinstance(info, dict) and info.get("role") == "user" and isinstance(info.get("id"), str) and info["id"]
        ):
            raise self._poison(
                seat_no,
                session_id,
                RuntimeError(f"OpenCode noReply seed 响应畸形：{str(resp)[:200]}"),
            )
        self._seeded_sessions.add(session_id)

    async def _list_v2_turn(
        self,
        session_id: str,
        seat_no: int,
        *,
        user_id: str,
        final_assistant_id: str,
    ) -> list[dict[str, Any]]:
        """完整枚举并验证本轮 assistant；无法证明完整时直接毒化。"""
        try:
            data = await self._request(
                "GET",
                f"/session/{session_id}/message",
                directory=self._directory_for_seat(seat_no),
            )
            if not isinstance(data, list):
                raise ValueError(f"消息列表不是 list：{type(data).__name__}")

            seen_ids: set[str] = set()
            user_count = 0
            assistants: list[dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("消息列表含非 object 条目")
                info = item.get("info")
                parts = item.get("parts")
                if not isinstance(info, dict) or not isinstance(parts, list):
                    raise ValueError("消息条目缺少良构 info/parts")
                if any(not isinstance(part, dict) for part in parts):
                    raise ValueError("消息 parts 含非 object 条目")
                message_id = info.get("id")
                role = info.get("role")
                if not isinstance(message_id, str) or not message_id or message_id in seen_ids:
                    raise ValueError("消息 id 缺失或重复")
                if role not in {"user", "assistant"}:
                    raise ValueError(f"消息 role 非法：{role!r}")
                if role == "assistant" and not isinstance(info.get("parentID"), str):
                    raise ValueError("assistant 缺少 parentID")
                seen_ids.add(message_id)
                if message_id == user_id:
                    if role != "user":
                        raise ValueError("同步响应 parentID 对应的不是 user")
                    user_count += 1
                if role == "assistant" and info.get("parentID") == user_id:
                    assistants.append(item)

            assistant_ids = [item["info"]["id"] for item in assistants]
            if user_count != 1:
                raise ValueError(f"当前 user 枚举次数不是 1：{user_count}")
            if final_assistant_id not in assistant_ids:
                raise ValueError("同步响应的最终 assistant 不在当前 user 子消息中")
            return assistants
        except BaseException as exc:
            if isinstance(exc, OpenCodeSessionPoisoned):
                raise
            poisoned = self._poison(seat_no, session_id, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise poisoned from exc

    @staticmethod
    def _agent_error(error: Any) -> BaseException | None:
        if not error:
            return None
        name = error.get("name") if isinstance(error, dict) else None
        if name == "StructuredOutputError":
            return RetryableActorError(f"模型未按 schema 产出 structured output：{error}")
        if name == "APIError":
            return RetryableActorError(f"OpenCode provider 请求失败（按瞬态重试）：{error}")
        return RuntimeError(f"OpenCode agent 错误：{error}")

    def _v2_envelope(
        self,
        assistants: list[dict[str, Any]],
        *,
        final_assistant_id: str,
        action_tool: str | None,
        speech_required: bool,
    ) -> str:
        """从当前 user 的完整 assistant 链提取 game-blind 文本/工具 envelope。"""
        final = next(msg for msg in assistants if msg["info"]["id"] == final_assistant_id)
        for msg in assistants:
            error = self._agent_error(msg["info"].get("error"))
            if error is not None:
                raise error

        calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for msg in assistants:
            for part in msg["parts"]:
                if not isinstance(part, dict):
                    raise RetryableActorError("OpenCode 本轮含畸形 tool part")
                if part.get("type") != "tool":
                    continue
                calls.append((msg, part))

        speech_source = final
        tool_payload: dict[str, Any] | None = None
        if action_tool is None:
            if calls:
                names = [part.get("tool") for _, part in calls]
                raise RetryableActorError(f"本阶段不允许工具调用，模型却调用了：{names}")
        else:
            if len(calls) != 1:
                raise RetryableActorError(f"本阶段必须恰好调用一次 {action_tool}，实际工具调用数：{len(calls)}")
            speech_source, part = calls[0]
            if part.get("tool") != action_tool:
                raise RetryableActorError(f"本阶段必须调用 {action_tool}，实际调用：{part.get('tool')!r}")
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") != "completed":
                status = state.get("status") if isinstance(state, dict) else None
                raise RetryableActorError(f"工具 {action_tool} 未成功完成（status={status!r}）")
            tool_input = state.get("input")
            if not isinstance(tool_input, dict):
                raise RetryableActorError(f"工具 {action_tool} 的 input 不是 object")
            tool_payload = {"name": action_tool, "input": tool_input}

        speech = self._v2_text(speech_source)
        if speech_required and not speech:
            raise RetryableActorError("本阶段要求自然语言发言，但对应 assistant 没有 text")

        envelope: dict[str, Any] = {}
        if speech:
            envelope["text"] = speech
        if tool_payload is not None:
            envelope["tool"] = tool_payload
        return json.dumps(envelope, ensure_ascii=False)

    async def _call_v2(
        self,
        *,
        session_id: str,
        seat_no: int,
        view: Any,
        text: str,
        model_ref: str,
        agent: str,
    ) -> ChatMessage:
        if not model_ref:
            raise ValueError("agent/tool v2 要求为座位显式配置 providerID/modelID")
        seed_prompt = getattr(view, "seed_prompt", None)
        if not isinstance(seed_prompt, str):
            raise TypeError(f"view.seed_prompt 必须是 str，收到 {type(seed_prompt)}")
        action_tool = getattr(view, "action_tool", None)
        if action_tool is not None and (not isinstance(action_tool, str) or not action_tool):
            raise TypeError(f"view.action_tool 必须是非空 str 或 None，收到 {action_tool!r}")
        speech_required = getattr(view, "speech_required", None)
        if type(speech_required) is not bool:
            raise TypeError(f"view.speech_required 必须是 bool，收到 {speech_required!r}")

        await self._seed_v2(
            session_id,
            seat_no,
            seed_prompt=seed_prompt,
            agent=agent,
            model_ref=model_ref,
        )
        tools = {"*": False}
        if action_tool is not None:
            tools[action_tool] = True
        resp = await self._send_turn(
            session_id,
            seat_no,
            text,
            model_ref,
            None,
            agent=agent,
            tools=tools,
        )
        info = resp["info"]
        assistants = await self._list_v2_turn(
            session_id,
            seat_no,
            user_id=info["parentID"],
            final_assistant_id=info["id"],
        )
        self._pending_turns[seat_no] = _PendingTurn(
            session_id=session_id,
            user_id=info["parentID"],
            assistant_ids=tuple(msg["info"]["id"] for msg in assistants),
        )
        content = self._v2_envelope(
            assistants,
            final_assistant_id=info["id"],
            action_tool=action_tool,
            speech_required=speech_required,
        )
        return ChatMessage(role="assistant", content=content)

    async def call(self, phase: str, view: Any, messages: list[ChatMessage]) -> ChatMessage:
        seat_no = getattr(view, "seat_no", None)
        if type(seat_no) is not int or seat_no <= 0:
            raise TypeError(f"view.seat_no 必须是正整数，收到 {seat_no!r}")
        agent = getattr(view, "agent", None)
        if agent is not None and not isinstance(agent, str):
            raise TypeError(f"view.agent 必须是 str 或 None，收到 {type(agent)}")
        session_id = await self._ensure_session(seat_no, agent_mode=bool(agent))
        self._raise_if_poisoned(seat_no)
        # 防御性清理上一轮未验收消息；正常 parse 失败会在 rollback 中立即完成。
        await self._cleanup_failed_turn(seat_no)

        text = self._messages_to_text(messages)
        model_ref = self._model_for_seat(seat_no)
        if agent:
            return await self._call_v2(
                session_id=session_id,
                seat_no=seat_no,
                view=view,
                text=text,
                model_ref=model_ref,
                agent=agent,
            )

        # 玩法层在 view 上挂的 JSON Schema；None 表示本局走纯文本协议（无结构化输出约束）。
        schema = getattr(view, "schema", None)
        if schema is not None and not isinstance(schema, dict):
            raise TypeError(f"view.schema 必须是 dict 或 None，收到 {type(schema)}")

        resp = await self._send_and_register(session_id, seat_no, text, model_ref, schema)

        info = resp["info"]
        error = self._agent_error(info.get("error"))
        if error is not None:
            # 同步响应已完成、消息 id 已知，回滚按 id 精确删除后重抽是干净的。
            raise error
        if schema is not None:
            structured = info.get("structured")
            if structured is None:
                # 与 StructuredOutputError 同类：回滚后重抽。
                raise RetryableActorError(f"OpenCode 回合完成但缺少 structured output（finish={info.get('finish')}）")
            return ChatMessage(
                role="assistant",
                content=json.dumps(structured, ensure_ascii=False),
            )
        response_text = self._extract_text(resp)
        if not response_text:
            raise RetryableActorError("OpenCode 响应中没有文本内容")
        return ChatMessage(role="assistant", content=response_text)

    def commit_turn(self, seat_no: int) -> None:
        """本轮产出已被调用方验收（parse 通过）：摘除失败标记，session 保留这轮对话。"""
        self._raise_if_poisoned(seat_no)
        self._pending_turns.pop(seat_no, None)

    async def rollback(self, seat_no: int) -> None:
        """本轮产出让调用方拒收（parse 失败）：立即从 session 删掉这轮的 user+assistant 消息。"""
        if seat_no in self._sessions:
            await self._cleanup_failed_turn(seat_no)

    async def close(self) -> None:
        await self._client.aclose()
