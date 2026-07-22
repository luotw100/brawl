"""OpenCode backend 的失败轮事务边界。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from core.opencode_runtime import OpenCodeConnection
from core.sr.errors import FatalActorError
from core.sr.messages import ChatMessage
from core.sr.opencode_backend import OpenCodeBackend, OpenCodeSessionPoisoned


def _message(
    msg_id: str,
    role: str,
    text: str,
    *,
    parent_id: str | None = None,
) -> dict[str, Any]:
    info: dict[str, Any] = {"id": msg_id, "role": role}
    if parent_id is not None:
        info.update({"parentID": parent_id, "finish": "stop"})
    return {"info": info, "parts": [{"type": "text", "text": text}]}


class _FakeOpenCode:
    """只实现这些测试需要的 session message 协议。"""

    def __init__(self) -> None:
        self.baseline = [
            _message("base-user", "user", "old prompt"),
            _message(
                "base-assistant",
                "assistant",
                "old response",
                parent_id="base-user",
            ),
        ]
        self.messages = deepcopy(self.baseline)
        self.prompt_posts = 0
        self.abort_posts = 0
        self.fail_discovery_once = False
        self.fail_delete_id: str | None = None
        self.fail_confirmation = False
        self.malformed_message_list_after_post = False
        self._fail_next_message_get = False
        self._cleanup_deleted_all = False

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(str(item["info"]["id"]) for item in self.messages)

    async def request(
        self,
        method: str,
        path: str,
        *,
        directory: str,
        **kwargs: Any,
    ) -> Any:
        assert directory.endswith("/seat-01")
        if method == "POST" and path.endswith("/prompt_async"):
            self.prompt_posts += 1
            prompt_no = self.prompt_posts
            text = kwargs["json"]["parts"][0]["text"]
            user_id = f"user-{prompt_no}"
            self.messages.extend(
                [
                    _message(user_id, "user", text),
                    _message(
                        f"assistant-{prompt_no}",
                        "assistant",
                        f"response-{prompt_no}",
                        parent_id=user_id,
                    ),
                ]
            )
            self._cleanup_deleted_all = False
            if self.fail_discovery_once:
                self.fail_discovery_once = False
                self._fail_next_message_get = True
            return None

        if method == "POST" and path.endswith("/abort"):
            self.abort_posts += 1
            return True

        if method == "DELETE" and "/message/" in path:
            msg_id = path.rsplit("/", 1)[-1]
            if msg_id == self.fail_delete_id:
                raise httpx.HTTPStatusError(
                    "delete failed",
                    request=httpx.Request(method, f"http://opencode{path}"),
                    response=httpx.Response(500),
                )
            self.messages = [item for item in self.messages if item["info"]["id"] != msg_id]
            self._cleanup_deleted_all = self.ids == tuple(
                item["info"]["id"] for item in self.baseline
            )
            return None

        if method == "GET" and path.endswith("/message"):
            if self._fail_next_message_get:
                self._fail_next_message_get = False
                raise httpx.ReadTimeout("message polling timed out")
            if self.malformed_message_list_after_post and self.prompt_posts:
                return {}
            if self.fail_confirmation and self._cleanup_deleted_all:
                raise httpx.ReadTimeout("rollback confirmation timed out")
            return deepcopy(self.messages)

        raise AssertionError(f"unexpected request: {method} {path}")


def _backend(server: _FakeOpenCode) -> OpenCodeBackend:
    backend = OpenCodeBackend(
        OpenCodeConnection(
            base_url="http://127.0.0.1:1",
            username="test",
            password="test",
            workspace_root="/workspace",
        ),
        poll_interval=0,
    )
    backend._sessions[1] = "session-1"
    backend._request = server.request  # type: ignore[method-assign]
    return backend


async def _call(backend: OpenCodeBackend) -> ChatMessage:
    return await backend.call(
        "test-turn",
        SimpleNamespace(seat_no=1),
        [ChatMessage(role="human", content="new prompt")],
    )


@pytest.mark.asyncio
async def test_post_accepted_then_get_timeout_is_rolled_back_before_retry() -> None:
    server = _FakeOpenCode()
    server.fail_discovery_once = True
    backend = _backend(server)
    try:
        with pytest.raises(httpx.ReadTimeout, match="message polling timed out"):
            await _call(backend)

        assert server.ids == ("base-user", "base-assistant")
        assert 1 not in backend._pending_turns
        assert 1 not in backend._poisoned_sessions

        response = await _call(backend)
        assert response.content == "response-2"
        assert server.prompt_posts == 2
        assert server.ids == (
            "base-user",
            "base-assistant",
            "user-2",
            "assistant-2",
        )
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_parse_rejection_restores_full_baseline_before_resubmit() -> None:
    server = _FakeOpenCode()
    backend = _backend(server)
    try:
        await _call(backend)
        await backend.rollback(1)

        assert server.ids == ("base-user", "base-assistant")
        assert 1 not in backend._pending_turns

        response = await _call(backend)
        assert response.content == "response-2"
        assert server.ids == (
            "base-user",
            "base-assistant",
            "user-2",
            "assistant-2",
        )
        backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_delete_failure_poison_session_and_preserves_pending_marker() -> None:
    server = _FakeOpenCode()
    backend = _backend(server)
    try:
        await _call(backend)
        marker = backend._pending_turns[1]
        server.fail_delete_id = "user-1"

        with pytest.raises(OpenCodeSessionPoisoned, match="无法确认已恢复事务基线") as exc_info:
            await backend.rollback(1)

        assert isinstance(exc_info.value, FatalActorError)
        assert backend._pending_turns[1] == marker
        assert 1 in backend._poisoned_sessions
        prompt_posts = server.prompt_posts
        with pytest.raises(OpenCodeSessionPoisoned, match="已被毒化"):
            await _call(backend)
        assert server.prompt_posts == prompt_posts
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_confirmation_failure_poison_session_even_if_messages_look_clean() -> None:
    server = _FakeOpenCode()
    backend = _backend(server)
    try:
        await _call(backend)
        marker = backend._pending_turns[1]
        server.fail_confirmation = True

        with pytest.raises(OpenCodeSessionPoisoned, match="rollback confirmation timed out"):
            await backend.rollback(1)

        assert server.ids == ("base-user", "base-assistant")
        assert backend._pending_turns[1] == marker
        assert 1 in backend._poisoned_sessions
        with pytest.raises(OpenCodeSessionPoisoned, match="已被毒化"):
            backend.commit_turn(1)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_cancellation_waits_for_transactional_cleanup() -> None:
    server = _FakeOpenCode()
    backend = _backend(server)
    waiting = asyncio.Event()

    async def wait_forever(
        session_id: str,
        seat_no: int,
        user_id: str,
    ) -> tuple[str, str]:
        assert (session_id, seat_no, user_id) == ("session-1", 1, "user-1")
        waiting.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    backend._wait_for_response = wait_forever  # type: ignore[method-assign]
    task = asyncio.create_task(_call(backend))
    try:
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert server.abort_posts == 1
        assert server.ids == ("base-user", "base-assistant")
        assert 1 not in backend._pending_turns
        assert 1 not in backend._poisoned_sessions
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await backend.close()


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"data": {}},
        {"messages": []},
        [None],
        [{}],
        [{"info": None}],
        [{"info": {"id": 1}}],
        [{"info": {"id": ""}}],
    ],
)
def test_transaction_message_ids_reject_malformed_success_bodies(data: Any) -> None:
    with pytest.raises(RuntimeError):
        OpenCodeBackend._message_ids(data)


def test_transaction_message_ids_accept_only_explicit_complete_lists() -> None:
    rows = [_message("user-1", "user", "prompt")]
    assert OpenCodeBackend._message_ids(rows) == ("user-1",)
    assert OpenCodeBackend._message_ids({"data": rows}) == ("user-1",)
    assert OpenCodeBackend._message_ids([]) == ()


@pytest.mark.asyncio
async def test_malformed_200_message_list_after_post_poison_session() -> None:
    server = _FakeOpenCode()
    server.malformed_message_list_after_post = True
    backend = _backend(server)
    try:
        with pytest.raises(OpenCodeSessionPoisoned, match="无法确认已恢复事务基线"):
            await _call(backend)

        assert server.prompt_posts == 1
        assert 1 in backend._pending_turns
        assert 1 in backend._poisoned_sessions
        with pytest.raises(OpenCodeSessionPoisoned, match="已被毒化"):
            await _call(backend)
        assert server.prompt_posts == 1
    finally:
        await backend.close()
