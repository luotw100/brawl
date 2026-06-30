"""API 边界：token 鉴权（只认 URL ?token=）+失败限流、公开频道隐私（无 replay 名/队列）、overlay 全链路、音频白名单。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server.app import _FAIL_LIMIT, create_app

API = "?token=t0k3n"  # console_token：控制 API 准入
WS = "/ws?token=st4ge"  # stage_token：观众频道准入


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_failed_auth_rate_limited_but_valid_token_never_blocked(client):
    for _ in range(_FAIL_LIMIT):
        assert client.get("/api/state?token=wrong").status_code == 401
    assert client.get("/api/state?token=wrong").status_code == 429
    # 正主拿对 token 不受任何影响——这是"别把自己锁外面"的硬保证
    assert client.get(f"/api/state{API}").status_code == 200


def test_api_requires_console_token(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state?token=st4ge").status_code == 401  # stage_token 进不了控制 API
    assert client.get(f"/api/state{API}").status_code == 200


def test_console_state_includes_ops_info(client):
    state = client.get(f"/api/state{API}").json()
    assert state["library"] == [{"id": "game1", "game": "avalon", "tags": ["TTS"]}]
    assert {"phase", "scene", "overlays", "queue", "knobs", "replay_id"} <= state.keys()
    assert state["knobs"]["feed_window"] == 2


def test_ws_requires_stage_token(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_text()


def test_ws_sends_state_and_never_leaks_replay_names(client):
    client.put(f"/api/queue{API}", json={"queue": ["game1"]})
    with client.websocket_connect(WS) as ws:
        raw = ws.receive_text()
        msg = json.loads(raw)
        assert msg["type"] == "state"
        assert {"ts", "phase", "scene", "overlays", "prefetch"} <= msg.keys()
        # 隐私边界：公开频道不出现队列与 replay 目录名
        assert "queue" not in msg and "library" not in msg and "replay_id" not in msg
        assert "game1" not in raw


def test_overlay_roundtrip_and_broadcast(client):
    with client.websocket_connect(WS) as ws:
        json.loads(ws.receive_text())  # 初始状态
        created = client.post(f"/api/overlays{API}", json={
            "kind": "marquee", "content": "感谢 @某观众 的火箭！", "y": 88.0, "ttl_seconds": None,
        }).json()
        assert created["id"]
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "state" and msg["overlays"][0]["content"].startswith("感谢")
        client.delete(f"/api/overlays/{created['id']}{API}")
        msg = json.loads(ws.receive_text())
        assert msg["overlays"] == []


def test_queue_rejects_unknown_replay(client):
    resp = client.put(f"/api/queue{API}", json={"queue": ["ghost"]})
    assert resp.status_code == 422
    assert client.put(f"/api/queue{API}", json={"queue": ["game1"]}).status_code == 200


def test_playback_and_knobs_validation(client):
    assert client.post(f"/api/playback{API}", json={"action": "step"}).status_code == 200
    assert client.post(f"/api/playback{API}", json={"action": "warp"}).status_code == 422
    assert client.post(f"/api/knobs{API}", json={"pace": 2.0}).status_code == 422
    assert client.post(f"/api/knobs{API}", json={"feed_window": 9}).status_code == 422
    assert client.post(f"/api/knobs{API}", json={"pace": 0.3, "feed_window": 1}).status_code == 200


def test_static_shell_requires_token(settings, tmp_path):
    # 壳也要门：裸链/错 token 取不到 index.html（数据没漏，但"画面"本身也不许裸奔）。
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    s = settings.model_copy(update={"web_dist": dist})
    with TestClient(create_app(s)) as c:
        assert c.get("/").status_code == 401
        assert c.get("/?token=wrong").status_code == 401
        # 两个 token 任一都能取壳（路由在 hash 里、服务端分不清，只校验是不是其一）
        assert c.get("/?token=st4ge").status_code == 200
        assert c.get("/?token=t0k3n").status_code == 200
        # /assets/* 仍公开：纯前端代码，<script src> 带不了 token，门住壳即足够
        assert c.get("/assets/app.js").status_code == 200


def test_replay_audio_served_but_nothing_else(client, settings):
    tts = settings.replay_root / "game1" / "tts"
    tts.mkdir(exist_ok=True)  # fixture 已建配音目录，这里只是再塞一条 wav 验证只发音频
    (tts / "1.1.host.opening.wav").write_bytes(b"RIFF")
    (tts / "manifest.json").write_text('{"x": 1}')
    (settings.replay_root / "game1" / "llm_calls.jsonl").write_text("secret prompts")
    audio = client.get("/replays/game1/tts/1.1.host.opening.wav")
    assert audio.content == b"RIFF"
    assert "immutable" in audio.headers["cache-control"]  # 预取后正式播放零网络往返
    # 只发 tts/*.{wav,mp3,...}：manifest/llm_calls/meta/events 含台词文本与配置，绝不上公网
    assert client.get("/replays/game1/tts/manifest.json").status_code == 404
    assert client.get("/replays/game1/llm_calls.jsonl").status_code == 404
    assert client.get("/replays/game1/tts/../llm_calls.jsonl").status_code == 404
    assert client.get("/replays/game1/tts/nope.wav").status_code == 404


def test_nested_replay_assets_and_queue_ids(client, settings):
    from server.tests.conftest import write_replay

    replay = write_replay(settings.replay_root / "AvalonGames", "001")
    audio = replay / "tts" / "1.1.host.opening.wav"
    audio.write_bytes(b"RIFF")
    assert client.put(f"/api/queue{API}", json={"queue": ["AvalonGames/001"]}).status_code == 200
    assert client.get("/replays/AvalonGames/001/tts/1.1.host.opening.wav").content == b"RIFF"
    assert client.get("/replays/AvalonGames/001/tts/manifest.json").status_code == 404
