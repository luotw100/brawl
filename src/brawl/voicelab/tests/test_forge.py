"""Voice forge 的 DashScope 响应与下载凭证边界。"""

from __future__ import annotations

import httpx
import pytest

from brawl.voicelab.forge import SettingsError, _post, _synthesize

_AUDIO_HOST = "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"


def test_post_turns_non_json_error_into_settings_error() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(502, text="bad gateway")
        )
    )
    with client, pytest.raises(SettingsError, match="502.*bad gateway"):
        _post(client, "https://dashscope.aliyuncs.com/example", {})


def test_synthesize_rejects_untrusted_download_before_request() -> None:
    api_client = httpx.Client(
        headers={"Authorization": "Bearer secret"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"output": {"audio": {"url": "https://attacker.example/key.wav"}}},
            )
        ),
    )

    def unexpected_download(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"不应请求未受信地址：{request.url}")

    audio_client = httpx.Client(transport=httpx.MockTransport(unexpected_download))
    with (
        api_client,
        audio_client,
        pytest.raises(SettingsError, match="可信音频地址"),
    ):
        _synthesize(api_client, audio_client, "voice", "text", "instructions")


def test_synthesize_downloads_https_without_api_authorization() -> None:
    api_requests: list[httpx.Request] = []
    audio_requests: list[httpx.Request] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        return httpx.Response(
            200,
            json={
                "output": {
                    "audio": {
                        "url": f"http://{_AUDIO_HOST}/sample.wav?signature=abc",
                    }
                }
            },
        )

    def audio_handler(request: httpx.Request) -> httpx.Response:
        audio_requests.append(request)
        return httpx.Response(200, content=b"wave")

    api_client = httpx.Client(
        headers={"Authorization": "Bearer secret"},
        transport=httpx.MockTransport(api_handler),
    )
    audio_client = httpx.Client(transport=httpx.MockTransport(audio_handler))
    with api_client, audio_client:
        result = _synthesize(api_client, audio_client, "voice", "text", "instructions")

    assert result == b"wave"
    assert api_requests[0].headers["Authorization"] == "Bearer secret"
    assert audio_requests[0].url.scheme == "https"
    assert audio_requests[0].url.host == _AUDIO_HOST
    assert "Authorization" not in audio_requests[0].headers
