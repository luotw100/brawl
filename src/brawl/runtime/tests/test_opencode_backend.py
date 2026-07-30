"""OpenCode backend 的 legacy StructuredOutput 与 agent/tool v2 协议。"""

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
    return httpx.HTTPStatusError(f"status {status}", request=request, response=httpx.Response(status, request=request))


class _FakeOpenCode:
    """只实现这些测试需要的同步 session message 协议。"""

    def __init__(self) -> None:
        self.turns = 0
        self.abort_posts = 0
        self.deleted: list[str] = []
        self.fail_delete_id: str | None = None
        self.fail_post: str | None = None  # "timeout" / "http500" / "http429"
        self.fail_seed: str | None = None  # "timeout" / "http429"
        self.malformed_seed = False
        self.error_payload: dict[str, Any] | None = None
        self.error_on_format: dict[str, Any] | None = None  # 仅当请求带 format 时报错
        self.structured: Any = {"answer": 1}
        self.text = "response"
        self.malformed = False
        self.last_payload: dict[str, Any] | None = None
        self.payloads: list[dict[str, Any]] = []
        self.block_post = False
        self.started = asyncio.Event()
        self.session_payload: dict[str, Any] | None = None
        self.providers: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.v2_assistants: list[dict[str, Any]] | None = None
        self.fail_get_messages = False
        self.malformed_messages: Any = None

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
        if method == "GET" and path.endswith("/message"):
            if self.fail_get_messages:
                raise _http_error(500)
            if self.malformed_messages is not None:
                return self.malformed_messages
            return self.history
        if method == "POST" and path.endswith("/session"):
            self.session_payload = kwargs.get("json")
            return {"id": "session-1"}
        if method == "POST" and path.endswith("/message"):
            payload = kwargs.get("json")
            self.last_payload = payload
            self.payloads.append(payload)
            if payload.get("noReply") is True:
                if self.fail_seed == "timeout":
                    raise httpx.ReadTimeout("seed timed out")
                if self.fail_seed == "http429":
                    raise _http_error(429)
                if self.malformed_seed:
                    return {"info": {"role": "user"}, "parts": payload["parts"]}
                seed = {
                    "info": {"id": "seed-user", "role": "user"},
                    "parts": payload["parts"],
                }
                self.history.append(seed)
                return seed
            if self.block_post:
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            self.turns += 1
            if self.fail_post == "timeout":
                raise httpx.ReadTimeout("sync post timed out")
            if self.fail_post == "http500":
                raise _http_error(500)
            if self.fail_post == "http429":
                raise _http_error(429)
            if self.malformed:
                return {"info": {"role": "assistant"}, "parts": []}
            turn = self.turns
            user_id = f"user-{turn}"
            user = {
                "info": {"id": user_id, "role": "user"},
                "parts": payload["parts"],
            }
            info: dict[str, Any] = {
                "id": f"assistant-{turn}",
                "role": "assistant",
                "parentID": user_id,
                "finish": "stop",
            }
            has_format = self.last_payload.get("format") is not None
            if self.error_on_format is not None and has_format:
                info["error"] = self.error_on_format
                return {"info": info, "parts": []}
            if self.error_payload is not None:
                info["error"] = self.error_payload
                assistant = {"info": info, "parts": []}
                self.history.extend([user, assistant])
                return assistant
            if has_format:
                info["structured"] = self.structured
            if self.v2_assistants is not None:
                assistants = self.v2_assistants
                self.history.extend([user, *assistants])
                return assistants[-1]
            assistant = {"info": info, "parts": [{"type": "text", "text": self.text}]}
            self.history.extend([user, assistant])
            return assistant

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


def _backend(server: _FakeOpenCode, *, default_model: str = "") -> OpenCodeBackend:
    backend = OpenCodeBackend(
        OpenCodeConnection(
            base_url="http://127.0.0.1:1",
            username="test",
            password="test",
            workspace_root="/workspace",
        ),
        default_model=default_model,
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


def _assistant(
    assistant_id: str,
    *,
    text: str = "",
    tools: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": assistant_id,
        "role": "assistant",
        "parentID": "user-1",
        "finish": "stop",
    }
    if error is not None:
        info["error"] = error
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(tools or [])
    return {"info": info, "parts": parts}


def _tool(
    name: str,
    *,
    status: str = "completed",
    tool_input: Any = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"status": status}
    if status == "completed":
        state["input"] = {"team": [1, 2]} if tool_input is None else tool_input
        state["output"] = "accepted"
    elif status == "error":
        state["input"] = {"team": [1, 2]} if tool_input is None else tool_input
        state["error"] = "invalid"
    return {
        "type": "tool",
        "tool": name,
        "callID": f"call-{name}",
        "state": state,
    }


def _v2_view(
    *,
    action_tool: str | None = "submit_team",
    speech_required: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        seat_no=1,
        agent="avalon-servant",
        seed_prompt="你是 1 号忠臣。",
        action_tool=action_tool,
        speech_required=speech_required,
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
async def test_v2_seeds_once_and_sends_explicit_agent_model_and_tool_policy() -> None:
    server = _FakeOpenCode()
    server.v2_assistants = [
        _assistant(
            "assistant-action",
            text="我提议一支稳健队伍。",
            tools=[_tool("submit_team")],
        ),
        _assistant("assistant-1", text="已提交。"),
    ]
    backend = _backend(server, default_model="deepseek/deepseek-chat")
    view = _v2_view()
    try:
        resp = await _call(backend, view)

        assert len(server.payloads) == 2
        seed, turn = server.payloads
        assert seed == {
            "parts": [{"type": "text", "text": "你是 1 号忠臣。"}],
            "noReply": True,
            "agent": "avalon-servant",
            "model": {"providerID": "deepseek", "modelID": "deepseek-chat"},
            "tools": {"*": False},
        }
        assert turn["agent"] == "avalon-servant"
        assert turn["model"] == {"providerID": "deepseek", "modelID": "deepseek-chat"}
        assert turn["tools"] == {"*": False, "submit_team": True}
        assert "format" not in turn
        assert json.loads(resp.content) == {
            "text": "我提议一支稳健队伍。",
            "tool": {"name": "submit_team", "input": {"team": [1, 2]}},
        }

        backend.commit_turn(1)
        server.v2_assistants = [_assistant("assistant-2", text="第二次发言。", tools=[_tool("submit_team")])]
        # fake 的第二轮 parentID 也要与其真实 user 对齐。
        server.v2_assistants[0]["info"]["parentID"] = "user-2"
        await _call(backend, view)
        assert len(server.payloads) == 3
        assert server.payloads[-1]["agent"] == "avalon-servant"
        assert server.payloads[-1]["model"] == {
            "providerID": "deepseek",
            "modelID": "deepseek-chat",
        }
        assert server.payloads[-1]["tools"] == {"*": False, "submit_team": True}
        assert "format" not in server.payloads[-1]
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_v2_no_action_tool_disables_all_tools_and_returns_final_text() -> None:
    server = _FakeOpenCode()
    server.v2_assistants = [_assistant("assistant-1", text="我赞成先听完所有人的理由。")]
    backend = _backend(server, default_model="deepseek/deepseek-chat")
    try:
        resp = await _call(
            backend,
            _v2_view(action_tool=None, speech_required=True),
        )
        assert server.payloads[-1]["tools"] == {"*": False}
        assert json.loads(resp.content) == {"text": "我赞成先听完所有人的理由。"}
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "http429", "malformed"])
async def test_v2_seed_failure_poisons_unknown_session_state(failure: str) -> None:
    server = _FakeOpenCode()
    if failure == "malformed":
        server.malformed_seed = True
    else:
        server.fail_seed = failure
    backend = _backend(server, default_model="deepseek/deepseek-chat")
    try:
        with pytest.raises(OpenCodeSessionPoisoned, match="无法确认干净"):
            await _call(backend, _v2_view())
        assert 1 in backend._poisoned_sessions
        if failure != "malformed":
            assert server.abort_posts == 1
        assert server.turns == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assistants", "match"),
    [
        ([_assistant("assistant-1", text="只说不做")], "实际工具调用数：0"),
        (
            [_assistant("assistant-1", text="错误工具", tools=[_tool("submit_vote")])],
            "实际调用.*submit_vote",
        ),
        (
            [
                _assistant(
                    "assistant-1",
                    text="重复调用",
                    tools=[_tool("submit_team"), _tool("submit_team")],
                )
            ],
            "实际工具调用数：2",
        ),
        (
            [
                _assistant(
                    "assistant-1",
                    text="工具失败",
                    tools=[_tool("submit_team", status="error")],
                )
            ],
            "未成功完成",
        ),
    ],
)
async def test_v2_bad_tool_use_is_retryable_and_fully_rollbackable(
    assistants: list[dict[str, Any]],
    match: str,
) -> None:
    server = _FakeOpenCode()
    server.v2_assistants = assistants
    backend = _backend(server, default_model="deepseek/deepseek-chat")
    try:
        with pytest.raises(RetryableActorError, match=match):
            await _call(backend, _v2_view())
        assert backend._pending_turns[1].assistant_ids == tuple(item["info"]["id"] for item in assistants)

        await backend.rollback(1)
        assert server.deleted == [
            *(item["info"]["id"] for item in reversed(assistants)),
            "user-1",
        ]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_v2_missing_required_speech_is_retryable() -> None:
    server = _FakeOpenCode()
    server.v2_assistants = [
        _assistant("assistant-action", tools=[_tool("submit_team")]),
        _assistant("assistant-1", text="工具调用后的确认文字不算提案发言。"),
    ]
    backend = _backend(server, default_model="deepseek/deepseek-chat")
    try:
        with pytest.raises(RetryableActorError, match="要求自然语言发言"):
            await _call(backend, _v2_view())
        await backend.rollback(1)
        assert server.deleted == ["assistant-1", "assistant-action", "user-1"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_v2_tool_can_carry_speech_without_assistant_text() -> None:
    server = _FakeOpenCode()
    server.v2_assistants = [
        _assistant(
            "assistant-action",
            tools=[
                _tool(
                    "submit_team",
                    tool_input={
                        "team": [1, 2],
                        "speech": "我提议一号和二号执行任务。",
                    },
                )
            ],
        ),
        _assistant("assistant-1", text="工具调用后的确认文本不属于公开发言。"),
    ]
    backend = _backend(server, default_model="deepseek/deepseek-chat")
    try:
        response = await _call(
            backend,
            _v2_view(speech_required=False),
        )

        assert json.loads(response.content) == {
            "tool": {
                "name": "submit_team",
                "input": {
                    "team": [1, 2],
                    "speech": "我提议一号和二号执行任务。",
                },
            },
        }
        backend.commit_turn(1)
        assert 1 not in backend._pending_turns
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "not-list", "missing-final", "bad-part"])
async def test_v2_get_failure_or_incomplete_enumeration_poisons(failure: str) -> None:
    server = _FakeOpenCode()
    server.v2_assistants = [_assistant("assistant-1", text="正常发言", tools=[_tool("submit_team")])]
    if failure == "http":
        server.fail_get_messages = True
    elif failure == "not-list":
        server.malformed_messages = {"messages": []}
    elif failure == "missing-final":
        server.malformed_messages = [
            {"info": {"id": "user-1", "role": "user"}, "parts": []},
        ]
    else:
        server.v2_assistants[0]["parts"].append("not-an-object")
    backend = _backend(server, default_model="deepseek/deepseek-chat")
    try:
        with pytest.raises(OpenCodeSessionPoisoned, match="无法确认干净"):
            await _call(backend, _v2_view())
        assert 1 in backend._poisoned_sessions
        assert 1 not in backend._pending_turns
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_v2_api_error_is_retryable_after_full_enumeration() -> None:
    server = _FakeOpenCode()
    server.v2_assistants = [
        _assistant(
            "assistant-1",
            error={
                "name": "APIError",
                "data": {"message": "upstream failed", "isRetryable": False},
            },
        )
    ]
    backend = _backend(server, default_model="deepseek/deepseek-chat")
    try:
        with pytest.raises(RetryableActorError, match="provider 请求失败"):
            await _call(backend, _v2_view())
        await backend.rollback(1)
        assert server.deleted == ["assistant-1", "user-1"]
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
async def test_api_error_is_retryable_and_rolls_back_cleanly() -> None:
    server = _FakeOpenCode()
    # 2026-07 真实事故载荷：zen 网关上游瞬时抖动也标 400 + isRetryable=false，
    # 这两个字段不可信，APIError 一律按瞬态重抽。
    server.error_payload = {
        "name": "APIError",
        "data": {
            "message": "Error from provider (Console Go): Upstream request failed",
            "statusCode": 400,
            "isRetryable": False,
        },
    }
    backend = _backend(server)
    try:
        with pytest.raises(RetryableActorError, match="provider 请求失败"):
            await _call(backend)

        await backend.rollback(1)
        assert server.deleted == ["assistant-1", "user-1"]

        server.error_payload = None
        resp = await _call(backend)
        assert resp.content == "response"
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_other_agent_error_stays_fatal() -> None:
    server = _FakeOpenCode()
    server.error_payload = {"name": "ProviderAuthError", "data": {"message": "bad key"}}
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
        await backend.assert_models_available(["opencode-go/deepseek-v4-pro", "opencode-go/glm-5.2", ""])
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_assert_models_available_raises_and_lists_missing() -> None:
    server = _FakeOpenCode()
    server.providers = [{"id": "opencode-go", "models": {"deepseek-v4-pro": {}}}]
    backend = _backend(server)
    try:
        with pytest.raises(OpenCodeRuntimeError, match=r"opencode-go/glm-9"):
            await backend.assert_models_available(["opencode-go/deepseek-v4-pro", "opencode-go/glm-9"])
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


@pytest.mark.asyncio
async def test_agent_session_creation_does_not_allow_structured_output() -> None:
    server = _FakeOpenCode()
    backend = _backend(server, default_model="deepseek/deepseek-v4-flash")
    backend._sessions.clear()
    server.v2_assistants = [
        _assistant(
            "assistant-1",
            text="1号和2号上任务。",
            tools=[_tool("submit_team")],
        )
    ]
    try:
        await _call(backend, _v2_view())
        assert server.session_payload is not None
        assert server.session_payload["permission"] == [
            {"permission": "*", "pattern": "*", "action": "deny"},
        ]
    finally:
        await backend.close()
