from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from core.actor import OpenCodeBackend


class _View:
    seat_no = 2


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

    response = await backend.call("probe", _View(), None, [HumanMessage(content="hello")])  # type: ignore[arg-type]

    assert response.content == '{"ok": true}'
    create_payload = next(payload for method, path, payload in calls if method == "POST" and path == "/session")
    assert create_payload == {"title": "game-seat-2"}
    prompt_payload = next(
        payload for method, path, payload in calls if method == "POST" and path == "/session/ses_test/prompt_async"
    )
    assert prompt_payload == {
        "parts": [{"type": "text", "text": "[user]\nhello"}],
        "model": {"providerID": "opencode-go", "modelID": "qwen3.7-plus"},
    }

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
