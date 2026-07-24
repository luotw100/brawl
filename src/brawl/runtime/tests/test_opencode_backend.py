"""OpenCode backend 的失败轮事务边界（同步 message 端点版）。

同步 POST /session/{id}/message 直接返回 assistant 消息（含 id/parentID/structured），
回滚只靠本地记账 id 的 DELETE；backend 不允许触碰 GET message 列表（format 会话上
该端点永远 400，见 opencode_backend 模块 docstring）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from brawl.runtime.errors import FatalActorError, RetryableActorError
from brawl.runtime.messages import ChatMessage
from brawl.runtime.opencode_backend import OpenCodeBackend, OpenCodeSessionPoisoned
from brawl.runtime.opencode_runtime import OpenCodeConnection, OpenCodeRuntimeError

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "number"}},
    "required": ["answer"],
}


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://opencode/session/x/message")
    return httpx.HTTPStatusError(
        f"status {status}", request=request, response=httpx.Response(status, request=request)
    )


class _FakeOpenCode:
    """只实现这些测试需要的同步 session message 协议。"""

    def __init__(self) -> None:
        self.turns = 0
        self.abort_posts = 0
        self.deleted: list[str] = []
        self.fail_delete_id: str | None = None
        self.fail_post: str | None = None  # "timeout" / "http500" / "http429"
        self.error_payload: dict[str, Any] | None = None
        self.error_on_format: dict[str, Any] | None = None  # 仅当请求带 format 时报错
        self.structured: Any = {"answer": 1}
        self.text = "response"
        self.malformed = False
        self.last_payload: dict[str, Any] | None = None
        self.block_post = False
        self.started = asyncio.Event()
        self.session_payload: dict[str, Any] | None = None
        self.providers: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        directory: str,
        **kwargs: Any,
    ) -> Any:
        if method == "GET" and path == "/config/providers":
            return {"providers": self.providers}
        assert directory.endswith("/seat-01")
        if method == "POST" and path.endswith("/session"):
            self.session_payload = kwargs.get("json")
            return {"id": "session-1"}
        if method == "POST" and path.endswith("/message"):
            if self.block_post:
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            self.turns += 1
            self.last_payload = kwargs.get("json")
            if self.fail_post == "timeout":
                raise httpx.ReadTimeout("sync post timed out")
            if self.fail_post == "http500":
                raise _http_error(500)
            if self.fail_post == "http429":
                raise _http_error(429)
            if self.malformed:
                return {"info": {"role": "assistant"}, "parts": []}
            turn = self.turns
            info: dict[str, Any] = {
                "id": f"assistant-{turn}",
                "role": "assistant",
                "parentID": f"user-{turn}",
                "finish": "stop",
            }
            has_format = self.last_payload.get("format") is not None
            if self.error_on_format is not None and has_format:
                info["error"] = self.error_on_format
                return {"info": info, "parts": []}
            if self.error_payload is not None:
                info["error"] = self.error_payload
                return {"info": info, "parts": []}
            if has_format:
                info["structured"] = self.structured
            return {"info": info, "parts": [{"type": "text", "text": self.text}]}

        if method == "POST" and path.endswith("/abort"):
            self.abort_posts += 1
            return True

        if method == "DELETE" and "/message/" in path:
            msg_id = path.rsplit("/", 1)[-1]
            if msg_id == self.fail_delete_id:
                raise _http_error(500)
            self.deleted.append(msg_id)
            return None

        raise AssertionError(f"unexpected request: {method} {path}")


def _backend(server: _FakeOpenCode) -> OpenCodeBackend:
    backend = OpenCodeBackend(
        OpenCodeConnection(
            base_url="http://127.0.0.1:1",
            username="test",
            password="test",
            workspace_root="/workspace",
        ),
    )
    backend._sessions[1] = "session-1"
    backend._request = server.request  # type: ignore[method-assign]
    return backend


async def _call(backend: OpenCodeBackend, view: Any = None) -> ChatMessage:
    return await backend.call(
        "test-turn",
        view if view is not None else SimpleNamespace(seat_no=1),
        [ChatMessage(role="human", content="new prompt")],
    )


@pytest.mark.asyncio
async def test_schema_view_sends_format_and_returns_structured_json() -> None:
    server = _FakeOpenCode()
    backend = _backend(server)
    try:
        resp = await _call(backend, SimpleNamespace(seat_no=1, schema=_SCHEMA))
        assert server.last_payload is not None
        assert server.last_payload["format"] == {"type": "json_schema", "schema": _SCHEMA}
        assert resp.content == json.dumps({"answer": 1}, ensure_ascii=False)
        assert 1 in backend._pending_turns
        backend.commit_turn(1)
        assert 1 not in backend._pending_turns
        assert server.deleted == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_plain_view_falls_back_to_text_protocol() -> None:
    server = _FakeOpenCode()
    backend = _backend(server)
    try:
        resp = await _call(backend)
        assert server.last_payload is not None
        assert "format" not in server.last_payload
        assert resp.content == "response"
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_parse_rejection_rollback_deletes_turn_messages() -> None:
    server = _FakeOpenCode()
    backend = _backend(server)
    try:
        await _call(backend)
        await backend.rollback(1)

        assert server.deleted == ["assistant-1", "user-1"]
        assert 1 not in backend._pending_turns

        resp = await _call(backend)
        assert resp.content == "response"
        assert server.turns == 2
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_structured_output_error_is_retryable_and_rolls_back_cleanly() -> None:
    server = _FakeOpenCode()
    server.error_on_format = {
        "name": "StructuredOutputError",
        "data": {"message": "Model did not produce structured output", "retries": 0},
    }
    backend = _backend(server)
    try:
        view = SimpleNamespace(seat_no=1, schema=_SCHEMA)
        with pytest.raises(RetryableActorError, match="structured output"):
            await _call(backend, view)

        await backend.rollback(1)
        assert server.deleted == ["assistant-1", "user-1"]

        server.error_on_format = None
        resp = await _call(backend, view)
        assert json.loads(resp.content) == {"answer": 1}
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_missing_structured_output_is_retryable() -> None:
    server = _FakeOpenCode()
    server.structured = None
    backend = _backend(server)
    try:
        with pytest.raises(RetryableActorError, match="缺少 structured output"):
            await _call(backend, SimpleNamespace(seat_no=1, schema=_SCHEMA))
        await backend.rollback(1)
        assert server.deleted == ["assistant-1", "user-1"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_other_agent_error_stays_fatal() -> None:
    server = _FakeOpenCode()
    server.error_payload = {"name": "APIError", "data": {"message": "provider boom"}}
    backend = _backend(server)
    try:
        with pytest.raises(RuntimeError, match="OpenCode agent 错误"):
            await _call(backend)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_transport_failure_poisons_session_and_blocks_retry() -> None:
    server = _FakeOpenCode()
    server.fail_post = "timeout"
    backend = _backend(server)
    try:
        with pytest.raises(OpenCodeSessionPoisoned, match="无法确认干净"):
            await _call(backend)

        assert server.abort_posts == 1
        assert 1 in backend._poisoned_sessions
        with pytest.raises(OpenCodeSessionPoisoned, match="已被毒化"):
            await _call(backend)
        with pytest.raises(OpenCodeSessionPoisoned, match="已被毒化"):
            backend.commit_turn(1)
        with pytest.raises(OpenCodeSessionPoisoned, match="已被毒化"):
            await backend.rollback(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_http_500_poisons_session() -> None:
    server = _FakeOpenCode()
    server.fail_post = "http500"
    backend = _backend(server)
    try:
        with pytest.raises(OpenCodeSessionPoisoned):
            await _call(backend)
        assert server.abort_posts == 1
        assert 1 in backend._poisoned_sessions
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_http_4xx_is_rejected_before_turn_and_not_poisoned() -> None:
    server = _FakeOpenCode()
    server.fail_post = "http429"
    backend = _backend(server)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await _call(backend)

        assert 1 not in backend._poisoned_sessions
        assert 1 not in backend._pending_turns
        assert server.abort_posts == 0

        server.fail_post = None
        resp = await _call(backend)
        assert resp.content == "response"
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_delete_failure_poisons_session_and_preserves_pending_marker() -> None:
    server = _FakeOpenCode()
    backend = _backend(server)
    try:
        await _call(backend)
        marker = backend._pending_turns[1]
        server.fail_delete_id = "user-1"

        with pytest.raises(OpenCodeSessionPoisoned, match="无法确认干净") as exc_info:
            await backend.rollback(1)

        assert isinstance(exc_info.value, FatalActorError)
        assert backend._pending_turns[1] == marker
        assert 1 in backend._poisoned_sessions
        with pytest.raises(OpenCodeSessionPoisoned, match="已被毒化"):
            await _call(backend)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_cancellation_during_sync_post_aborts_and_poisons() -> None:
    server = _FakeOpenCode()
    server.block_post = True
    backend = _backend(server)
    task = asyncio.create_task(_call(backend))
    try:
        await server.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert server.abort_posts == 1
        assert 1 in backend._poisoned_sessions
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await backend.close()


@pytest.mark.asyncio
async def test_malformed_response_poisons_session() -> None:
    server = _FakeOpenCode()
    server.malformed = True
    backend = _backend(server)
    try:
        with pytest.raises(OpenCodeSessionPoisoned, match="无法确认干净"):
            await _call(backend)
        assert 1 in backend._poisoned_sessions
        with pytest.raises(OpenCodeSessionPoisoned, match="已被毒化"):
            await _call(backend)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_assert_models_available_passes_when_all_present() -> None:
    server = _FakeOpenCode()
    server.providers = [
        {"id": "opencode-go", "models": {"deepseek-v4-pro": {}, "glm-5.2": {}}},
        "not-a-dict",  # 坏形状条目静默跳过，不影响良构 provider
        {"id": 42, "models": {"x": {}}},  # provider id 非字符串同样跳过
    ]
    backend = _backend(server)
    try:
        await backend.assert_models_available(
            ["opencode-go/deepseek-v4-pro", "opencode-go/glm-5.2", ""]
        )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_assert_models_available_raises_and_lists_missing() -> None:
    server = _FakeOpenCode()
    server.providers = [{"id": "opencode-go", "models": {"deepseek-v4-pro": {}}}]
    backend = _backend(server)
    try:
        with pytest.raises(OpenCodeRuntimeError, match=r"opencode-go/glm-9"):
            await backend.assert_models_available(
                ["opencode-go/deepseek-v4-pro", "opencode-go/glm-9"]
            )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_assert_models_available_skips_empty_refs() -> None:
    """空引用 = 用 server 默认模型，必须整体跳过、不发起任何请求。"""
    server = _FakeOpenCode()  # 无 providers；一旦发请求 fake 会因路径不匹配而炸
    backend = _backend(server)
    try:
        await backend.assert_models_available(["", ""])
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_session_creation_denies_all_but_structured_output() -> None:
    """通配 deny 会连 StructuredOutput 工具一起拒掉（1.17.20 实测），必须精确放行。"""
    server = _FakeOpenCode()
    backend = _backend(server)
    backend._sessions.clear()  # 触发真实 _create_session
    try:
        await _call(backend)
        assert server.session_payload is not None
        assert server.session_payload["permission"] == [
            {"permission": "*", "pattern": "*", "action": "deny"},
            {"permission": "StructuredOutput", "pattern": "*", "action": "allow"},
        ]
    finally:
        await backend.close()
