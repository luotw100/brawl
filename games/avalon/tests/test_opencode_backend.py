from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from avalon.player import AgentPlayer, PlayerView
from avalon.state import Role
from core.actor import OpenCodeBackend
from core.opencode_runtime import OpenCodeConnection
from core.recorder import NullRecorder
from core.threads import ThreadMessage


class _View:
    seat_no = 2


_LINEUP = [Role.MERLIN, Role.PERCIVAL, Role.SERVANT, Role.ASSASSIN, Role.MORGANA]


def _connection() -> OpenCodeConnection:
    return OpenCodeConnection(
        base_url="http://127.0.0.1:45678",
        username="opencode",
        password="test-secret",
        workspace_root="/oc/workspaces",
    )


def _player_view(*, messages: list[ThreadMessage] | None = None, prompt: str = "新事件") -> PlayerView:
    return PlayerView(
        seat_no=1,
        role=Role.MERLIN,
        intel=[4, 5],
        model=None,
        lineup=list(_LINEUP),
        players=5,
        leader_no=1,
        quest_no=1,
        attempt=1,
        team_size=2,
        fails_required=1,
        succeeded=0,
        failed=0,
        team=[1, 2],
        lady_used=[],
        messages=messages or [],
        prompt=prompt,
    )


class _CapturingOpenCodeBackend(OpenCodeBackend):
    def __init__(self) -> None:
        super().__init__(_connection())
        self.sent_texts: list[str] = []

    async def call(self, phase: str, view: PlayerView, spec: Any, messages: list[BaseMessage]) -> AIMessage:
        self.sent_texts.append(str(messages[0].content))
        self._sessions.setdefault(view.seat_no, "ses_test")
        return AIMessage(content='{"phase":"discuss","monologue":"先压一下","speech":"这队先看票型，我暂时不急着下结论。"}')


class _FailOnceOpenCodeBackend(_CapturingOpenCodeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def call(self, phase: str, view: PlayerView, spec: Any, messages: list[BaseMessage]) -> AIMessage:
        self.sent_texts.append(str(messages[0].content))
        if not self.failed:
            self.failed = True
            raise RuntimeError("provider failed")
        return AIMessage(content='{"phase":"discuss","monologue":"重试成功","speech":"继续。"}')


async def test_opencode_backend_sends_prompt_async_with_current_model_shape(monkeypatch) -> None:
    backend = OpenCodeBackend(
        _connection(),
        default_model="opencode-go/deepseek-v4-flash",
        seat_models={2: "opencode-go/qwen3.7-plus"},
    )
    calls: list[tuple[str, str, str | None, dict[str, Any] | None]] = []
    prompt_sent = False

    async def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        nonlocal prompt_sent
        calls.append((method, path, kwargs.get("directory"), kwargs.get("json")))
        if method == "POST" and path == "/session":
            return {"id": "ses_test"}
        if method == "POST" and path == "/session/ses_test/prompt_async":
            prompt_sent = True
            return None
        if method == "GET" and path == "/session/ses_test/message":
            if not prompt_sent:
                return []
            return [
                {
                    "info": {
                        "id": "msg_user",
                        "role": "user",
                        "time": {"created": 1783350000000},
                    },
                    "parts": [{"type": "text", "text": "hello"}],
                },
                {
                    "info": {
                        "id": "msg_assistant",
                        "role": "assistant",
                        "parentID": "msg_user",
                        "finish": "stop",
                        "time": {"created": 1783350000000, "completed": 1783350001000},
                    },
                    "parts": [{"type": "text", "text": '{"ok": true}'}],
                },
            ]
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(backend, "_request", fake_request)

    assert backend.has_session(2) is False
    response = await backend.call("probe", _View(), None, [HumanMessage(content="hello")])  # type: ignore[arg-type]

    assert response.content == '{"ok": true}'
    assert backend.has_session(2) is True
    create_payload = next(payload for method, path, _, payload in calls if method == "POST" and path == "/session")
    assert create_payload == {
        "title": "game-seat-2",
        "permission": [{"permission": "*", "pattern": "*", "action": "deny"}],
    }
    prompt_payload = next(
        payload for method, path, _, payload in calls if method == "POST" and path == "/session/ses_test/prompt_async"
    )
    assert prompt_payload == {
        "parts": [{"type": "text", "text": "hello"}],
        "model": {"providerID": "opencode-go", "modelID": "qwen3.7-plus"},
    }
    assert {
        directory
        for _, path, directory, _ in calls
        if path.startswith("/session")
    } == {"/oc/workspaces/seat-02"}

    await backend.close()


async def test_opencode_backend_accepts_reasoning_only_json_object() -> None:
    backend = OpenCodeBackend(_connection())

    assert backend._extract_text(
        {
            "parts": [
                {"type": "step-start"},
                {"type": "reasoning", "text": '  {"phase":"team_vote","approve":true}  '},
                {"type": "step-finish"},
            ]
        }
    ) == '{"phase":"team_vote","approve":true}'

    await backend.close()


async def test_opencode_backend_ignores_non_object_reasoning() -> None:
    backend = OpenCodeBackend(_connection())

    assert backend._extract_text({"parts": [{"type": "reasoning", "text": "先分析局势，再投票。"}]}) == ""
    assert backend._extract_text({"parts": [{"type": "reasoning", "text": "[true]"}]}) == ""

    await backend.close()


async def test_opencode_backend_cleans_up_failed_turn_on_retry(monkeypatch) -> None:
    backend = OpenCodeBackend(_connection(), default_model="opencode-go/mimo-v2.5")
    backend._sessions[2] = "ses_test"
    backend._last_user[2] = "msg_old_user"
    prompt_sent = False

    messages: list[dict[str, Any]] = [
        {
            "info": {"id": "msg_old_user", "role": "user", "time": {"created": 1783350000000}},
            "parts": [{"type": "text", "text": "old prompt"}],
        },
        {
            "info": {
                "id": "msg_old_error",
                "role": "assistant",
                "parentID": "msg_old_user",
                "time": {"created": 1783350000000},
                "error": {"name": "UnknownError", "data": {"message": "stale"}},
            },
            "parts": [],
        },
    ]

    async def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        nonlocal prompt_sent
        if method == "GET" and path == "/session/ses_test/message":
            return list(messages)
        if method == "POST" and path == "/session/ses_test/abort":
            return True
        if method == "DELETE" and path.startswith("/session/ses_test/message/"):
            msg_id = path.split("/")[-1]
            messages[:] = [m for m in messages if m["info"]["id"] != msg_id]
            return True
        if method == "POST" and path == "/session/ses_test/prompt_async":
            prompt_sent = True
            messages.append(
                {
                    "info": {"id": "msg_user", "role": "user", "time": {"created": 1783350001000}},
                    "parts": [{"type": "text", "text": "hello"}],
                }
            )
            messages.append(
                {
                    "info": {
                        "id": "msg_new_reply",
                        "role": "assistant",
                        "parentID": "msg_user",
                        "finish": "stop",
                        "time": {"created": 1783350001000, "completed": 1783350002000},
                    },
                    "parts": [{"type": "text", "text": '{"ok": true}'}],
                }
            )
            return None
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(backend, "_request", fake_request)

    response = await backend.call("probe", _View(), None, [HumanMessage(content="hello")])  # type: ignore[arg-type]

    assert response.content == '{"ok": true}'
    assert prompt_sent is True
    assert backend._last_user.get(2) is None
    # 旧失败消息已被清理，只剩新的 user + assistant
    assert [m["info"]["id"] for m in messages] == ["msg_user", "msg_new_reply"]

    await backend.close()


async def test_opencode_backend_ignores_stale_session_error_on_retry(monkeypatch) -> None:
    backend = OpenCodeBackend(_connection(), default_model="opencode-go/mimo-v2.5")
    backend._sessions[2] = "ses_test"
    prompt_sent = False

    old_error = {
        "info": {
            "id": "msg_old_error",
            "role": "assistant",
            "time": {"created": 1783350000000},
            "error": {"name": "UnknownError", "data": {"message": "unknown certificate verification error"}},
        }
    }
    new_user = {
        "info": {
            "id": "msg_user",
            "role": "user",
            "time": {"created": 1783350002000},
        },
        "parts": [{"type": "text", "text": "hello"}],
    }
    new_reply = {
        "info": {
            "id": "msg_new_reply",
            "role": "assistant",
            "parentID": "msg_user",
            "finish": "stop",
            "time": {"created": 1783350002000, "completed": 1783350003000},
        },
        "parts": [{"type": "text", "text": '{"ok": true}'}],
    }

    async def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        nonlocal prompt_sent
        if method == "GET" and path == "/session/ses_test/message":
            return [old_error, new_user, new_reply] if prompt_sent else [old_error]
        if method == "POST" and path == "/session/ses_test/prompt_async":
            prompt_sent = True
            return None
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(backend, "_request", fake_request)

    response = await backend.call("probe", _View(), None, [HumanMessage(content="hello")])  # type: ignore[arg-type]

    assert response.content == '{"ok": true}'
    assert prompt_sent is True

    await backend.close()


async def test_agent_player_sends_opencode_context_once_then_incremental() -> None:
    backend = _CapturingOpenCodeBackend()
    player = AgentPlayer(recorder=NullRecorder(), backend=backend)

    await player.discuss(_player_view(prompt="第一轮事件"))
    await player.discuss(
        _player_view(
            prompt="第二轮事件",
            messages=[
                ThreadMessage(role="human", content="上一轮问题"),
                ThreadMessage(role="assistant", content='{"speech":"上一轮发言","monologue":"上一轮内心"}'),
            ],
        )
    )

    first, second = backend.sent_texts
    assert "我是这局《阿瓦隆》的主持人" in first
    assert "【你的固定设定】" in first
    assert "梅林硬约束" in first

    assert "沿用本 session 开局时给你的身份" in second
    assert "第二轮事件" in second
    assert "【你的固定设定】" not in second
    assert "【你的私人历史】" not in second
    assert "梅林硬约束" not in second
    assert "上一轮问题" not in second

    await backend.close()


async def test_new_opencode_session_full_seeds_checkpoint_private_history() -> None:
    backend = _CapturingOpenCodeBackend()
    player = AgentPlayer(recorder=NullRecorder(), backend=backend)
    view = _player_view(
        prompt="续跑后的新事件",
        messages=[
            ThreadMessage(role="human", content="断点前主持人问题"),
            ThreadMessage(role="assistant", content='{"speech":"断点前发言","monologue":"旧判断"}'),
        ],
    )

    await player.discuss(view)

    text = backend.sent_texts[0]
    assert "【你的固定设定】" in text
    assert "【你的私人历史】" in text
    assert "断点前主持人问题" in text
    assert "断点前发言" in text
    assert "续跑后的新事件" in text
    await backend.close()


async def test_failed_first_opencode_turn_reseeds_full_context() -> None:
    backend = _FailOnceOpenCodeBackend()
    player = AgentPlayer(recorder=NullRecorder(), backend=backend)
    view = _player_view(
        prompt="首个事件",
        messages=[ThreadMessage(role="human", content="开局私人历史")],
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        await player._say("discuss", view)
    await player._say("discuss", view)
    await player._say("discuss", _player_view(prompt="后续事件"))

    first, retry, incremental = backend.sent_texts
    assert "【你的固定设定】" in first
    assert "【你的固定设定】" in retry
    assert "开局私人历史" in retry
    assert "【你的固定设定】" not in incremental
    assert "沿用本 session 开局时给你的身份" in incremental
    await backend.close()


async def test_opencode_backend_lists_models_from_config_providers(monkeypatch) -> None:
    backend = OpenCodeBackend(_connection())

    async def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        assert (method, path, kwargs) == (
            "GET",
            "/config/providers",
            {"directory": "/oc/workspaces"},
        )
        return {
            "providers": [
                {
                    "id": "opencode-go",
                    "models": {
                        "deepseek-v4-flash": {"id": "deepseek-v4-flash", "providerID": "opencode-go"},
                        "qwen3.7-plus": {"id": "qwen3.7-plus", "providerID": "opencode-go"},
                    },
                }
            ]
        }

    monkeypatch.setattr(backend, "_request", fake_request)

    assert await backend.list_models() == [
        {"id": "deepseek-v4-flash", "providerID": "opencode-go"},
        {"id": "qwen3.7-plus", "providerID": "opencode-go"},
    ]

    await backend.close()
