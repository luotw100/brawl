"""stage-record：把一局 replay 离线录成视频文件（无人值守、自起自销）。

与 live Docker 栈完全解耦——不连那个长跑频道，而是**自己起一个一次性 stage 引擎**：
进程内起 uvicorn 到 127.0.0.1:<随机口> → Playwright 无头 Chromium 开 #/stage 录画面 →
读 engine.phase 由 playing→intermission 判定本局放完即停 → 收每拍时间线 → ffmpeg 把
replay 里的 .opus 按拍偏移拼成音轨、与画面 mux 成 mp4。

音画对齐靠引擎的确定性：每拍时长由 engine._duration 算死、引擎实时 sleep 的就是这些时长，
所以"按 started_at 折算的音频偏移"与"画面里这拍实际出现的时刻"天然对齐。以第一拍为零点，
把视频前面的 idle 留白裁掉；残余的 WS→渲染常数延迟用 --audio-shift 微调。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright
from pydantic import ValidationError

from server.app import create_app
from server.record_settings import RENDER_DEVICE, RecordSettings
from server.settings import StageSettings

_VIEWPORT = {"width": 1080, "height": 1920}  # 竖屏，与 OBS 画布同口径
_TAIL_SECONDS = 1.0  # 终局定格后多录一点，保证最后一帧落进视频文件


@dataclass
class Beat:
    """一拍的录制时间线条目：seq 去重，started_at 定位偏移，audio_url 指向待拼的音频。"""

    seq: int
    started_at: float
    audio_url: str | None


# ---------------------------------------------------------------- 纯函数（可单测）


def audio_segments(beats: list[Beat], replay_root: Path, shift: float) -> list[tuple[Path, float]]:
    """有声拍 → (opus 文件路径, 在视频里的起始秒)。以第一拍为零点；偏移加 shift 后下钳到 0。

    audio_url 形如 /replays/<id>/tts/<file>，剥掉 /replays/ 前缀即 replay_root 下的相对路径
    （与 app.py 的 get_audio 静态路由同一套构造，反过来落回文件系统）。"""
    if not beats:
        return []
    t0 = beats[0].started_at
    segs: list[tuple[Path, float]] = []
    for b in beats:
        if not b.audio_url:
            continue
        path = replay_root / b.audio_url.removeprefix("/replays/")
        segs.append((path, max(0.0, b.started_at - t0 + shift)))
    return segs


def ffmpeg_audio_cmd(segs: list[tuple[Path, float]], out_audio: Path) -> list[str]:
    """把各 .opus 按偏移 adelay 后 amix 成一条音轨。normalize=0：发言基本不重叠，不要自动压音量。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for path, _ in segs:
        cmd += ["-i", str(path)]
    delays = [f"[{i}:a]adelay={int(off * 1000)}:all=1[a{i}]" for i, (_, off) in enumerate(segs)]
    labels = "".join(f"[a{i}]" for i in range(len(segs)))
    filt = ";".join(delays) + ";" + labels + f"amix=inputs={len(segs)}:normalize=0:dropout_transition=0[aout]"
    cmd += ["-filter_complex", filt, "-map", "[aout]", str(out_audio)]
    return cmd


def ffmpeg_mux_cmd(video: Path, audio: Path | None, lead: float, out: Path, encoder: str, fps: int) -> list[str]:
    """裁掉视频前 lead 秒（对齐到第一拍）+ 叠音轨 + 转码成 mp4。audio=None 时只转视频。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if encoder == "vaapi":
        cmd += ["-vaapi_device", RENDER_DEVICE]
    cmd += ["-ss", f"{lead:.3f}", "-i", str(video)]
    if audio is not None:
        cmd += ["-i", str(audio)]
    if encoder == "vaapi":
        cmd += ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-b:v", "6M"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"]
    cmd += ["-r", str(fps), "-map", "0:v:0"]
    if audio is not None:
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += [str(out)]
    return cmd


# ---------------------------------------------------------------- 引擎探针 / 等待


def _tap_engine(engine) -> list[Beat]:
    """包住 engine._publish：每拍 emit 后顺手把时间线记下来（seq 去重，暂停/旋钮的重发不计）。"""
    beats: list[Beat] = []
    seen: set[int] = set()
    orig = engine._publish

    async def tap() -> None:
        feed = engine.scene.feed
        if feed and feed[-1].seq not in seen:
            fi = feed[-1]
            seen.add(fi.seq)
            beats.append(Beat(fi.seq, fi.started_at, getattr(fi.directive, "audio_url", None)))
        await orig()

    engine._publish = tap
    return beats


async def _wait(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError(f"等 {what} 超时（{timeout}s）")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------- 采集（起栈 + 录屏）


async def _capture(settings: RecordSettings, video_dir: Path) -> tuple[list[Beat], float, Path]:
    """起一次性 stage + Playwright 录这一局的画面。返回 (时间线, 视频前导留白秒, webm 路径)。"""
    stage_settings = StageSettings(
        replay_root=settings.dir.parent,
        stage_token=secrets.token_urlsafe(8),
        console_token=secrets.token_urlsafe(8),
        web_dist=settings.web_dist,
        pace=settings.pace,
        loop_pool=False,  # 只放队列里这一局，放完即转幕间，绝不自动补别的局
    )
    app = create_app(stage_settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    try:
        await _wait(lambda: server.started, timeout=30, what="服务启动")
        port = server.servers[0].sockets[0].getsockname()[1]
        engine = app.state.engine
        beats = _tap_engine(engine)
        replay_id = settings.dir.name
        url = f"http://127.0.0.1:{port}/?token={stage_settings.stage_token}#/stage"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=not settings.headed,
                args=["--force-device-scale-factor=1", "--hide-scrollbars",
                      "--mute-audio", "--autoplay-policy=no-user-gesture-required"],
            )
            context = await browser.new_context(
                viewport=_VIEWPORT,
                record_video_dir=str(video_dir),
                record_video_size=_VIEWPORT,
            )
            page = await context.new_page()
            video = page.video
            t_origin = time.time()  # 视频时间轴起点（context/page 创建 ≈ 此刻）
            await page.goto(url)

            engine.queue = [replay_id]  # 入队这一局，主循环 1s 内拾起
            await _wait(lambda: engine.phase == "playing", timeout=60, what="开局")
            # 主停录信号：终局（phase 离开 playing）；次级兜底：到时长上限即停
            cap = time.monotonic() + settings.limit_seconds if settings.limit_seconds else None
            await _wait(lambda: engine.phase != "playing" or (cap is not None and time.monotonic() >= cap),
                        timeout=3600, what="终局")
            await asyncio.sleep(_TAIL_SECONDS)

            await context.close()  # 落盘视频文件
            video_path = Path(await video.path())
            await browser.close()

        lead = max(0.0, beats[0].started_at - t_origin) if beats else 0.0
        return beats, lead, video_path
    finally:
        server.should_exit = True
        await server_task


# ---------------------------------------------------------------- 编排


def _run(cmd: list[str]) -> None:
    logging.info("$ %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


async def _record(settings: RecordSettings) -> None:
    with tempfile.TemporaryDirectory(prefix="stage-record-") as tmp:
        tmpdir = Path(tmp)
        beats, lead, video = await _capture(settings, tmpdir)
        segs = audio_segments(beats, settings.dir.parent, settings.audio_shift)
        audio: Path | None = None
        if segs:
            audio = tmpdir / "audio.m4a"
            _run(ffmpeg_audio_cmd(segs, audio))
        _run(ffmpeg_mux_cmd(video, audio, lead, settings.out, settings.encoder, settings.fps))
    print(f"已生成：{settings.out}")


def main(argv: list[str]) -> int:
    """`stage-record` 的薄入口：argv 全交给 RecordSettings 的 CLI source。

    配置错误（坏参数 / 校验失败）只打友好消息、不甩 traceback（与 games/cli.py 同口径）。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        settings = RecordSettings.load(cli_args=argv)
    except ValidationError as e:
        msg = "；".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        print(f"配置错误: {msg}", file=sys.stderr)
        return 1
    asyncio.run(_record(settings))
    return 0


def main_cli() -> None:
    """console_script 零参入口：从 sys.argv 取参数转交 main。"""
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    main_cli()
