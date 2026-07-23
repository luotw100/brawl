"""record 内部一次性服务：静态页 + 本局 replay 媒体 + WS StateMsg。

只跑在 127.0.0.1 随机口、生命周期随一次录制，故无 token、无控制 API、无片库：
浏览器取页面与音频，WS 收 StateMsg，仅此而已。直播回归时在同一个 hub/protocol 上
另起带鉴权与调度层的服务装配，不复用本文件。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from brawl.stage.hub import Hub
from brawl.stage.protocol import StateMsg

_MEDIA_BUCKETS = {
    "tts": (".wav", ".mp3", ".opus", ".m4a"),
    "avatars": (".png", ".webp", ".jpg", ".jpeg"),
}


def _safe_media(replay_dir: Path, asset_path: str) -> Path | None:
    """replay 产物里只暴露 tts 音频与 avatars 头像（meta/sentences 含全量 prompt 与台词，不暴露）。
    路径统一 resolve 后限制在本局目录内，不存在即 None。"""
    parts = Path(asset_path).parts
    if len(parts) != 2:
        return None
    bucket, filename = parts
    exts = _MEDIA_BUCKETS.get(bucket)
    if exts is None or any(p in ("", ".", "..") for p in parts) or not filename.endswith(exts):
        return None
    root = replay_dir.resolve()
    path = root.joinpath(bucket, filename).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def create_record_app(
    replay_dir: Path,
    web_dist: Path,
    hub: Hub,
    state_for: Callable[[], StateMsg],
) -> FastAPI:
    """装配一次性录制服务。state_for 由装配层给（引擎当前状态 → StateMsg）。"""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.websocket("/ws")
    async def ws_channel(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text(state_for().model_dump_json())
        await hub.join(ws)
        try:
            while True:
                await ws.receive_text()  # 页面不上行业务消息；这里只为感知断开
        except WebSocketDisconnect:
            pass
        finally:
            await hub.leave(ws)

    # call_id 唯一、内容永不变：长缓存 + immutable，页面预取后正式播放零网络往返。
    @app.get("/replay/{asset_path:path}")
    async def get_replay_asset(asset_path: str) -> FileResponse:
        path = _safe_media(replay_dir, asset_path)
        if path is None:
            raise HTTPException(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app
