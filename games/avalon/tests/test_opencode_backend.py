from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from avalon.player import AgentPlayer, PlayerView
from avalon.state import Role
from core.actor import OpenCodeBackend
from core.recorder import NullRecorder
from core.threads import ThreadMessage


class _View:
    seat_no = 2


_LINEUP = [Role.MERLIN, Role.PERCIVAL, Role.SERVANT, Role.ASSASSIN, Role.MORGANA]


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
        super().__init__()
        self.sent_texts: list[str] = []

    async def call(self, phase: str, view: PlayerView, spec: Any, messages: list[BaseMessage]) -> AIMessage:
        self.sent_texts.append(str(messages[0].content))
        self._sessions.setdefault(view.seat_no, "ses_test")
        return AIMessage(content='{"phase":"discuss","monologue":"先压一下","speech":"这队先看票型，我暂时不急着下结论。"}')


async def test_opencode_backend_sends_prompt_async_with_current_model_shape(monkeypatch) -> None:
    backend = OpenCodeBackend(
        default_model="opencode-go/deepseek-v4-flash",
        seat_models={2: "opencode-go/qwen3.7-plus"},
    )
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    prompt_sent = False

    async def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        nonlocal prompt_sent
        calls.append((method, path, kwargs.get("json")))
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
                        "id": "msg_assistant",
                        "role": "assistant",
                        "finish": "stop",
                        "time": {"created": 1783350000000, "completed": 1783350001000},
                    },
                    "parts": [{"type": "text", "text": '{"ok": true}'}],
                }
            ]
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(backend, "_request", fake_request)

    assert backend.has_session(2) is False
    response = await backend.call("probe", _View(), None, [HumanMessage(content="hello")])  # type: ignore[arg-type]

    assert response.content == '{"ok": true}'
    assert backend.has_session(2) is True
    create_payload = next(payload for method, path, payload in calls if method == "POST" and path == "/session")
    assert create_payload == {"title": "game-seat-2"}
    prompt_payload = next(
        payload for method, path, payload in calls if method == "POST" and path == "/session/ses_test/prompt_async"
    )
    assert prompt_payload == {
        "parts": [{"type": "text", "text": "hello"}],
        "model": {"providerID": "opencode-go", "modelID": "qwen3.7-plus"},
    }

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


async def test_opencode_backend_lists_models_from_config_providers(monkeypatch) -> None:
    backend = OpenCodeBackend()

    async def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        assert (method, path, kwargs) == ("GET", "/config/providers", {})
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
