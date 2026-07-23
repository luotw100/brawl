"""OpenCode headless server 后端与协议。

通过 HTTP API 与 `opencode serve` 通信：每个座位一个 session，保留该座位记忆。
调用方渲染好最终 prompt 文本后交给本后端，由它发送并等待 assistant 回复。

传输层约定（opencode 1.17.20 实测确认）：

- 一律走同步 ``POST /session/{id}/message``：一次调用阻塞到生成结束，响应里直接
  带着完整 assistant 消息（``info.id`` / ``info.parentID`` / ``info.structured``）。
- 绝不调用 ``GET /session/{id}/message``：只要 session 里落过一条带 ``format``
  的 user 消息，该列表端点就会因为服务端自己的读侧校验 bug 永远 400（服务端落盘时
  给 format 注入 ``retryCount``，读侧 schema 又拒绝这个字段）。因此本后端不维护
  任何「先 GET 列消息」的基线/轮询逻辑，事务回滚只用同步响应里返回的
  user/assistant 消息 id 做本地记账。
- 玩法层在 view 上挂 ``schema``（JSON Schema dict）时，请求携带
  ``format: {"type": "json_schema", "schema": ...}``，服务端强制模型通过
  StructuredOutput 工具回结构化结果，响应 ``info.structured`` 直接是解析好的
  对象。``format.retryCount`` 在 1.17.20 是死配置（收下但从不使用），不传。
  模型没产出 structured output 属于内容失败，抛 RetryableActorError 交给外层重抽；
  不支持 format 的模型会直接报错（fatal），选模型时自行避开。
- 同步请求没有拿到完整响应（超时/传输错误/取消）时，服务端可能已创建本回合消息，
  而上面的 400 bug 使我们无法枚举这些孤儿消息——无法证明 session 干净，只能
  尽力 abort 后毒化 session，禁止原地重试，走 journal resume 恢复。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from core.opencode_runtime import OpenCodeConnection
from core.sr.errors import FatalActorError, RetryableActorError
from core.sr.messages import ChatMessage


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

    user_id / assistant_id 都来自同步响应本身（``info.parentID`` / ``info.id``），
    回滚时按 id 精确删除，不需要（也不可能）再 GET 列表确认。
    """

    session_id: str
    user_id: str
    assistant_id: str


class OpenCodeBackend:
    """OpenCode headless server 后端。

    通过 HTTP API 与 `opencode serve` 通信：
    - 每个座位一个 session，保留该座位记忆
    - 每次决策发送调用方渲染好的一段文本；是否全量 seed 或只发增量由玩法层决定
    - 同步 `POST /session/{id}/message` 直接拿回完整 assistant 消息

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
            # 先全量 deny 玩家 session 的一切工具/权限，再单独放行 StructuredOutput：
            # 1.17.20 上通配 deny 会连 StructuredOutput 工具一起拒掉（format 会话直接
            # StructuredOutputError）；后面的规则覆盖前面的，这条精确放行不影响其它工具。
            "permission": [
                {"permission": "*", "pattern": "*", "action": "deny"},
                {"permission": "StructuredOutput", "pattern": "*", "action": "allow"},
            ],
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

    async def _send_turn(
        self,
        session_id: str,
        seat_no: int,
        text: str,
        model_ref: str,
        schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """同步发送一轮对话，返回完整响应（含 info 与 parts）。

        拿不到完整响应时（超时/传输错误/取消），服务端可能已创建本回合消息，
        而 format 会话的消息列表端点永远 400、无法枚举孤儿消息——无法证明
        session 干净，尽力 abort 后毒化（见模块 docstring）。
        """
        directory = self._directory_for_seat(seat_no)
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if model_ref:
            payload["model"] = self._parse_ref(model_ref)
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
        if not (
            isinstance(user_id, str)
            and user_id
            and isinstance(assistant_id, str)
            and assistant_id
        ):
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
            try:
                await self._request(
                    "POST",
                    f"/session/{session_id}/abort",
                    directory=self._directory_for_seat(seat_no),
                )
            except Exception:
                pass

        task = asyncio.create_task(abort())
        # 清理不能被调用方取消到一半；取消由调用方自己的异常路径传播。
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass

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
                RuntimeError(
                    f"pending turn 属于 session {pending.session_id}，"
                    f"当前却是 {self._sessions.get(seat_no)}"
                ),
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
        """先删 assistant（子）再删 user（父），把未验收回合从 session 里抹掉。"""
        session_id = pending.session_id
        directory = self._directory_for_seat(seat_no)
        await self._request(
            "DELETE",
            f"/session/{session_id}/message/{pending.assistant_id}",
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
            f"OpenCode seat {seat_no} session {session_id} 无法确认干净（消息列表端点"
            f"对 format 会话不可用，无法枚举/校验残留消息），禁止原地重试：{reason}"
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
        """发送一轮并落本地 pending marker：调用方 parse 过才 commit_turn 摘除；
        parse 不过 rollback 按 id 删除，失败轮绝不留在 session 里污染下次上下文。"""
        resp = await self._send_turn(session_id, seat_no, text, model_ref, schema)
        info = resp["info"]
        self._pending_turns[seat_no] = _PendingTurn(
            session_id=session_id,
            user_id=info["parentID"],
            assistant_id=info["id"],
        )
        return resp

    async def call(self, phase: str, view: Any, messages: list[ChatMessage]) -> ChatMessage:
        seat_no = getattr(view, "seat_no", 0)
        session_id = await self._ensure_session(seat_no)
        self._raise_if_poisoned(seat_no)
        # 防御性清理上一轮未验收消息；正常 parse 失败会在 rollback 中立即完成。
        await self._cleanup_failed_turn(seat_no)

        text = self._messages_to_text(messages)
        model_ref = self._model_for_seat(seat_no)
        # 玩法层在 view 上挂的 JSON Schema；None 表示本局走纯文本协议（如狼人杀）。
        schema = getattr(view, "schema", None)
        if schema is not None and not isinstance(schema, dict):
            raise TypeError(f"view.schema must be dict or None, got {type(schema)}")

        resp = await self._send_and_register(session_id, seat_no, text, model_ref, schema)

        info = resp["info"]
        error = info.get("error")
        if error:
            # 模型没调 StructuredOutput 工具属于内容失败：消息 id 已知，回滚后
            # 重抽是干净的；其余 agent 错误维持旧的 fatal 语义。
            name = error.get("name") if isinstance(error, dict) else None
            if name == "StructuredOutputError":
                raise RetryableActorError(f"模型未按 schema 产出 structured output：{error}")
            raise RuntimeError(f"OpenCode agent error: {error}")
        if schema is not None:
            structured = info.get("structured")
            if structured is None:
                # 与 StructuredOutputError 同类：回滚后重抽。
                raise RetryableActorError(
                    f"OpenCode 回合完成但缺少 structured output（finish={info.get('finish')}）"
                )
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


__all__ = [
    "ModelBackend",
    "OpenCodeBackend",
    "OpenCodeSessionPoisoned",
]
