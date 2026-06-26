"""FastAPI 装配：公开 WS 频道（只推 StateMsg）+ 导播台控制 API + 静态托管。

鉴权边界：准入只认 URL 里的 `?token=`（运营者复制带 token 的链接即用，零输入框）。
/ws 要求 token == stage_token；/api/* 要求 token == console_token；取静态壳（GET /）
要求 token 是两者之一；以上共用同一套失败限流。唯 tts 音频公开（低敏台词语音，且
<audio>/预取带不了 token）；壳里无数据，全靠受控通道推。
隐私边界：公开频道只有舞台状态；replay 目录名、队列、片库只进 /api/state。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.engine import Engine, PlaybackGate
from server.hub import Hub
from server.overlay import OverlayBoard
from server.protocol import OverlayItem, StateMsg
from server.settings import StageSettings

_FAIL_WINDOW = 60.0  # 失败限流滑动窗口（秒）
_FAIL_LIMIT = 30  # 窗口内允许的错 token 尝试数


class PlaybackAction(BaseModel):
    action: str  # skip | step


class Knobs(BaseModel):
    pace: float | None = None
    speed: float | None = None
    intermission_seconds: float | None = None
    loop_pool: bool | None = None
    feed_window: int | None = None


class QueuePayload(BaseModel):
    queue: list[str]


def create_app(settings: StageSettings, playback_gate: PlaybackGate | None = None) -> FastAPI:
    hub = Hub()

    def state_msg() -> StateMsg:
        return StateMsg(phase=engine.phase,
                        scene=engine.public_scene(), overlays=overlays.items(),
                        prefetch=engine.prefetch)

    async def publish() -> None:
        await hub.broadcast(state_msg())

    engine = Engine(settings, publish, playback_gate=playback_gate)
    overlays = OverlayBoard(publish)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(engine.run())
        yield
        task.cancel()

    # 公网服务不送 API 地图：docs/openapi 全关
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.overlays = overlays

    # 失败限流：只数错 token 的尝试（滑动窗口）。带对 token 的请求永远不受限——
    # 链路经 frp 透传后看不到真实客户端 IP，按 IP 限会把运营者和攻击者锁成一锅；
    # 按"失败"限则穷举被钉死在每分钟 _FAIL_LIMIT 次，而正主始终畅通。
    failures: deque[float] = deque()

    def record_failure() -> bool:
        """记一笔失败并清窗口；返回是否已触发限流。"""
        now = time.monotonic()
        while failures and now - failures[0] > _FAIL_WINDOW:
            failures.popleft()
        if len(failures) >= _FAIL_LIMIT:
            return True
        failures.append(now)
        return False

    def require(expected: str):
        """HTTP 准入：URL ?token= 必须等于 expected，否则限流后 401/429。"""
        def dep(token: str | None = None) -> None:
            if token == expected:
                return
            raise HTTPException(status_code=429 if record_failure() else 401,
                                detail="token 不对")
        return dep

    guarded = [Depends(require(settings.console_token))]

    # ---------------------------------------------------------------- 观众 WS 频道

    @app.websocket("/ws")
    async def ws_channel(ws: WebSocket, token: str | None = None) -> None:
        if token != settings.stage_token:
            record_failure()
            await ws.close(code=1008)  # policy violation
            return
        await ws.accept()
        await ws.send_text(state_msg().model_dump_json())
        await hub.join(ws)
        try:
            while True:
                await ws.receive_text()  # 页面不上行业务消息；这里只为感知断开
        except WebSocketDisconnect:
            pass
        finally:
            await hub.leave(ws)

    # ---------------------------------------------------------------- 控制 API

    @app.get("/api/state", dependencies=guarded)
    async def get_state() -> dict:
        # 导播台专属视图：公开状态 + 运营信息（replay 目录名只在这里出现）
        return {
            **state_msg().model_dump(),
            "replay_id": engine.replay_id,
            "queue": list(engine.queue),
            "library": engine.library(),
            "knobs": {
                "pace": engine.pace,
                "speed": engine.speed,
                "intermission_seconds": engine.intermission_seconds,
                "loop_pool": engine.loop_pool,
                "feed_window": engine.feed_window,
            },
        }

    @app.post("/api/playback", dependencies=guarded)
    async def post_playback(payload: PlaybackAction) -> dict:
        match payload.action:
            case "skip":
                await engine.skip()
            case "step":
                await engine.step()
            case _:
                raise HTTPException(status_code=422, detail=f"未知动作：{payload.action}")
        return {"ok": True}

    @app.post("/api/knobs", dependencies=guarded)
    async def post_knobs(payload: Knobs) -> dict:
        if payload.pace is not None and not 0.0 <= payload.pace <= 1.0:
            raise HTTPException(status_code=422, detail="pace 必须在 0~1 之间")
        if payload.speed is not None and payload.speed <= 0:
            raise HTTPException(status_code=422, detail="speed 必须为正")
        if payload.feed_window is not None and not 1 <= payload.feed_window <= 3:
            raise HTTPException(status_code=422, detail="feed_window 必须在 1~3 之间")
        await engine.set_knobs(**payload.model_dump(exclude_none=True))
        return {"ok": True}

    @app.put("/api/queue", dependencies=guarded)
    async def put_queue(payload: QueuePayload) -> dict:
        library = set(engine.library())
        unknown = [r for r in payload.queue if r not in library]
        if unknown:
            raise HTTPException(status_code=422, detail=f"不在片库里：{unknown}")
        await engine.set_queue(payload.queue)
        return {"ok": True}

    @app.post("/api/overlays", dependencies=guarded)
    async def post_overlay(item: OverlayItem) -> OverlayItem:
        return await overlays.put(item)

    @app.delete("/api/overlays/{item_id}", dependencies=guarded)
    async def delete_overlay(item_id: str) -> dict:
        await overlays.remove(item_id)
        return {"ok": True}

    # ---------------------------------------------------------------- 静态

    # replay 产物里只暴露 tts 音频文件（events/llm_calls/meta/manifest 含全量 prompt、
    # 配置、台词文本，不上公网）。扩展名白名单挡掉 manifest.json；路径段由路由模板切分，
    # 天然无 ".." 穿越；不存在即 404。
    @app.get("/replays/{replay_id}/tts/{filename}")
    async def get_audio(replay_id: str, filename: str) -> FileResponse:
        path = settings.replay_root / replay_id / "tts" / filename
        if not (filename.endswith((".wav", ".mp3", ".opus", ".m4a")) and path.is_file()):
            raise HTTPException(status_code=404)
        # call_id 唯一、内容永不变：长缓存 + immutable，页面预取后正式播放零网络往返。
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # 厂商头像：录制时按座位烧进 avatars/<座位号>.png。同 tts，扩展名白名单 + 不存在即 404；
    # 内容随该局固定不变，长缓存 + immutable。
    @app.get("/replays/{replay_id}/avatars/{filename}")
    async def get_avatar(replay_id: str, filename: str) -> FileResponse:
        path = settings.replay_root / replay_id / "avatars" / filename
        if not (filename.endswith((".png", ".webp", ".jpg", ".jpeg")) and path.is_file()):
            raise HTTPException(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    if settings.web_dist is not None:
        index_html = settings.web_dist / "index.html"

        # 壳也要门：取 index.html 必须带是两个 token 之一的 ?token=，裸链/错 token 直接 401
        # （与数据通道共用失败限流）。路由 #/stage|#/console 在 hash 里、服务端看不见，故
        # 只校验"是不是其一"，分页门控交给前端 + 各自数据通道（WS/api）。显式 "/" 路由先于
        # 下面的 mount 注册，故精确命中 "/"；/assets/* 落到 mount 仍公开——那是纯前端代码，
        # <script src> 带不了 token，门住壳即足够。
        @app.get("/")
        async def index(token: str | None = None) -> FileResponse:
            if token not in (settings.stage_token, settings.console_token):
                raise HTTPException(status_code=429 if record_failure() else 401,
                                    detail="token 不对")
            return FileResponse(index_html)

        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")

    return app


def main() -> None:
    import uvicorn

    settings = StageSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
