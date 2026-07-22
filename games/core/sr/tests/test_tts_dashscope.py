"""DashScope TTS 的 API/OSS 凭证边界与下载地址校验。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import core.sr.tts as tts_mod
from core.sr.tts import DashScopeTTSBackend, TTSError

_AUDIO_HOST = "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"


def test_dashscope_rejects_invalid_constructor_configuration() -> None:
    with pytest.raises(TTSError, match="API key"):
        DashScopeTTSBackend("  ")
    with pytest.raises(ValueError, match="attempts"):
        DashScopeTTSBackend("secret-key", attempts=0)
    with pytest.raises(ValueError, match="rps"):
        DashScopeTTSBackend("secret-key", rps=0)


@pytest.mark.asyncio
async def test_rate_limiter_wraps_every_retry_attempt(monkeypatch) -> None:
    attempts_by_text: dict[str, int] = {}

    def api_handler(request: httpx.Request) -> httpx.Response:
        text = request.read().decode()
        attempts_by_text[text] = attempts_by_text.get(text, 0) + 1
        if attempts_by_text[text] == 1:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={"output": {"audio": {"url": f"https://{_AUDIO_HOST}/audio.wav"}}},
        )

    class _Clock:
        def __init__(self) -> None:
            self.calls = 0

        async def wait_next(self) -> None:
            self.calls += 1

    async def no_backoff(_seconds: float) -> None:
        return None

    backend = DashScopeTTSBackend(
        "secret-key",
        attempts=2,
        api_transport=httpx.MockTransport(api_handler),
        audio_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"wav")),
    )
    clock = _Clock()
    backend._clock = clock
    monkeypatch.setattr(tts_mod.asyncio, "sleep", no_backoff)

    await asyncio.gather(
        backend.synthesize("first", "voice"),
        backend.synthesize("second", "voice"),
    )

    assert clock.calls == 4


@pytest.mark.asyncio
async def test_dashscope_download_upgrades_https_without_authorization() -> None:
    api_requests: list[httpx.Request] = []
    audio_requests: list[httpx.Request] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": {
                    "audio": {
                        "url": f"http://{_AUDIO_HOST}/result/audio.wav?signature=abc",
                    }
                }
            },
        )

    def audio_handler(request: httpx.Request) -> httpx.Response:
        audio_requests.append(request)
        return httpx.Response(200, content=b"wave-bytes")

    backend = DashScopeTTSBackend(
        "secret-key",
        attempts=1,
        api_transport=httpx.MockTransport(api_handler),
        audio_transport=httpx.MockTransport(audio_handler),
    )
    result = await backend.synthesize("你好", "voice-id")

    assert result == b"wave-bytes"
    assert len(api_requests) == 1
    assert api_requests[0].headers["Authorization"] == "Bearer secret-key"
    assert len(audio_requests) == 1
    assert audio_requests[0].url.scheme == "https"
    assert audio_requests[0].url.host == _AUDIO_HOST
    assert audio_requests[0].url.query == b"signature=abc"
    assert "Authorization" not in audio_requests[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/audio.wav",
        "https://example.com/audio.wav",
        f"https://{_AUDIO_HOST}.example.com/audio.wav",
        f"https://user:password@{_AUDIO_HOST}/audio.wav",
        f"https://{_AUDIO_HOST}:8443/audio.wav",
    ],
)
async def test_dashscope_rejects_untrusted_audio_url_before_download(url: str) -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {"audio": {"url": url}}})

    def unexpected_download(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"不应下载未受信地址：{request.url}")

    backend = DashScopeTTSBackend(
        "secret-key",
        attempts=1,
        api_transport=httpx.MockTransport(api_handler),
        audio_transport=httpx.MockTransport(unexpected_download),
    )
    with pytest.raises(TTSError, match="音频下载地址"):
        await backend.synthesize("你好", "voice-id")


@pytest.mark.asyncio
async def test_dashscope_post_error_status_is_not_parsed_as_result() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"output": {"audio": {"url": "ignored"}}})

    backend = DashScopeTTSBackend(
        "secret-key",
        attempts=1,
        api_transport=httpx.MockTransport(api_handler),
        audio_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    with pytest.raises(TTSError, match="DashScope 请求失败"):
        await backend.synthesize("你好", "voice-id")


@pytest.mark.asyncio
async def test_dashscope_does_not_retry_non_retryable_status() -> None:
    requests = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, json={"message": "bad key"})

    backend = DashScopeTTSBackend(
        "secret-key",
        attempts=3,
        api_transport=httpx.MockTransport(api_handler),
        audio_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    with pytest.raises(TTSError, match="DashScope 请求失败"):
        await backend.synthesize("你好", "voice-id")

    assert requests == 1


@pytest.mark.asyncio
async def test_dashscope_does_not_retry_structurally_invalid_result() -> None:
    requests = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, text="not json")

    backend = DashScopeTTSBackend(
        "secret-key",
        attempts=3,
        api_transport=httpx.MockTransport(api_handler),
        audio_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    with pytest.raises(TTSError, match="不是合法 JSON"):
        await backend.synthesize("你好", "voice-id")

    assert requests == 1


@pytest.mark.asyncio
async def test_dashscope_download_error_status_is_not_returned_as_audio() -> None:
    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"output": {"audio": {"url": f"https://{_AUDIO_HOST}/forbidden.wav"}}},
        )

    def audio_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"not audio")

    backend = DashScopeTTSBackend(
        "secret-key",
        attempts=1,
        api_transport=httpx.MockTransport(api_handler),
        audio_transport=httpx.MockTransport(audio_handler),
    )
    with pytest.raises(TTSError, match="DashScope 请求失败"):
        await backend.synthesize("你好", "voice-id")
