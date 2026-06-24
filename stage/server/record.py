"""stage-record：把一局 replay 离线录成视频文件（无人值守、自起自销）。

与 live Docker 栈完全解耦——不连那个长跑频道，而是**自己起一个一次性 stage 引擎**：
进程内起 uvicorn 到 127.0.0.1:<随机口> → Xvfb 造一块虚拟屏（无需物理显示器，SSH 里照跑）→
带头 Chromium kiosk 把 #/stage 真渲染进这块虚拟 framebuffer → **ffmpeg x11grab 实时抓裸帧、
单趟硬编 HEVC（边播边编，编码藏进本就实时的播放里，无臃肿中间件、无"VP8→转码"的来回有损）**
→ 先等本局真上屏、首帧画定才开抓（待机/幕间挡在窗口外），读终局拍的定格收尾时刻判定放完即停
（赶在幕间下发渲染前收手，phase 离开 playing 兜底坏局）→ 收每拍时间线 → 把 replay 里的
.opus 按拍偏移离线拼成音轨 → 与已编好的视频流 `-c:v copy` remux 成 mp4（瞬时）。

音画对齐靠引擎的确定性 + 抓屏的挂钟 PTS：x11grab 的帧 PTS 以"开抓那一刻"为零点，引擎随后
实时 sleep 出每拍，所以"按 started_at 减开抓时刻折算的音频偏移"与"画面里这拍实际出现的 PTS"
天然对齐（origin = 开抓时刻 = 视频零点）；残余的常数延迟用 --audio-shift 微调。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from playwright.async_api import async_playwright
from pydantic import ValidationError
from rich.console import Console

from server.app import create_app
from server.protocol import (
    Assassination,
    Deal,
    Directive,
    GameOpen,
    GameOver,
    HostSpeech,
    Intermission,
    QuestReveal,
    SeatInfo,
    Speech,
    VoteReveal,
)
from server.bgm import BGM_PLAYLIST, BGM_VOLUME
from server.record_settings import RENDER_DEVICE, RecordSettings
from server.sfx import SFX_VOLUME, cues_for
from server.settings import EngineKnobs, StageSettings

_CAPTURE_WARMUP = 0.5  # 开抓后让 x11grab 真正挂上画面再进停录判定（此刻画面已是开局静帧，warmup 帧即开局、不黑、不挪 origin）
_FIRST_FRAME_SETTLE = 0.4  # 等开局首帧渲染落定再开抓：把待机/幕间挡在录制窗口之外，开抓即开局

_WEB_SRC = Path(__file__).resolve().parent.parent / "web"  # 前端源码目录（stage/web）：每次录制现构建

_console = Console()  # 进度输出：录制节拍 + ffmpeg 步骤（与捕获/编码同一条命令行）


@dataclass
class Beat:
    """一拍的录制时间线条目：seq 去重，started_at 定位偏移，audio_url 指向待拼的人声，
    cues 是这拍要叠的音效（名字 + 拍内偏移秒，tap 时由 cues_for 算好）。"""

    seq: int
    started_at: float
    audio_url: str | None
    cues: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class Timeline:
    """录制时间线：逐拍条目 + 终局定格的收尾挂钟时刻。

    gameover_ends_at = 终局拍 started_at + duration，即定格演完、幕间即将下发的那一刻；停录以它
    为准、赶在幕间渲染前收手。没有终局拍（坏局/跳局/limit 截断）时为 None，停录回落到
    phase 离开 playing 的兜底信号。"""

    beats: list[Beat] = field(default_factory=list)
    gameover_ends_at: float | None = None


# ---------------------------------------------------------------- 进度（节拍渲染）

_PHASE = {"propose": "提案", "discuss": "讨论", "lady": "验人", "banter": "闲聊"}


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _snip(text: str, n: int = 36) -> str:
    """台词压成一行预览：折叠空白、超长截断（节拍是进度而非全文）。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _seat_name(seats: list[SeatInfo], seat_no: int) -> str:
    return next((s.name for s in seats if s.no == seat_no), f"{seat_no}号")


def _beat_summary(d: Directive, seats: list[SeatInfo]) -> tuple[str, str, str]:
    """一拍 → (标签, 颜色, 详情)，给 rich 打成一行进度。"""
    match d:
        case GameOpen():
            return "开局", "cyan", f"{d.players} 人 · 善 {d.good} / 恶 {d.evil}"
        case Deal():
            return "发牌", "cyan", f"队长 {d.leader_no} 号"
        case HostSpeech():
            return "主持", "yellow", f"{d.name}：{_snip(d.text)}"
        case Speech():
            return _PHASE.get(d.phase, d.phase), "green", f"{_seat_name(seats, d.seat_no)}：{_snip(d.text)}"
        case VoteReveal():
            return "表决", "magenta", f"{'通过' if d.approved else '否决'} · 第 {d.quest}.{d.attempt} 队"
        case QuestReveal():
            return "任务", "blue", f"第 {d.quest} 任务 {d.result}（{d.fails} 失败）"
        case Assassination():
            return "刺杀", "red", "命中" if d.hit else "落空"
        case GameOver():
            return "终局", "bold red", f"{d.winner} 胜 · {d.win_reason}"
        case Intermission():
            return "幕间", "dim", ""
        case _:
            return "?", "white", type(d).__name__


# ---------------------------------------------------------------- 纯函数（可单测）


def audio_segments(
    beats: list[Beat], replay_root: Path, sfx_dir: Path, shift: float, origin: float
) -> list[tuple[Path, float, float]]:
    """各拍 → (opus 文件路径, 在视频里的起始秒, 增益)。人声增益 1.0；音效压在 SFX_VOLUME。
    以 origin（= x11grab 开抓时刻 = 视频零点）为零点折算偏移：base = started_at − origin + shift，
    下钳到 0。音效偏移在拍起点上再加 cue 的拍内偏移。

    audio_url 形如 /replays/<id>/tts/<file>，剥掉 /replays/ 前缀即 replay_root 下的相对路径
    （与 app.py 的 get_audio 静态路由同一套构造，反过来落回文件系统）；音效在 sfx_dir/<name>.opus。"""
    segs: list[tuple[Path, float, float]] = []
    for b in beats:
        base = b.started_at - origin + shift
        if b.audio_url:
            segs.append((replay_root / b.audio_url.removeprefix("/replays/"), max(0.0, base), 1.0))
        for name, at in b.cues:
            segs.append((sfx_dir / f"{name}.opus", max(0.0, base + at), SFX_VOLUME))
    return segs


def _atempo_chain(speed: float) -> str:
    """speed → ffmpeg atempo 滤镜串（含尾逗号，可直接前缀拼接）。speed=1.0 返回空串（不提速）。

    atempo 单次只支持 0.5~2.0，越界拆成连乘（如 2.5×→atempo=2.0,atempo=1.25）。atempo 保音高，
    所以提速不公鸭嗓。"""
    if speed == 1.0:
        return ""
    factors: list[float] = []
    s = speed
    while s > 2.0:
        factors.append(2.0); s /= 2.0
    while s < 0.5:
        factors.append(0.5); s *= 2.0
    factors.append(s)
    return "".join(f"atempo={f:g}," for f in factors)


def ffmpeg_audio_cmd(segs: list[tuple[Path, float, float]], out_audio: Path, speed: float = 1.0) -> list[str]:
    """把各 .opus 按偏移 adelay、按增益 volume 后 amix 成一条音轨。normalize=0：发言基本不
    重叠、音效已压低，不要自动压音量（否则人声会被错峰盖章拽下去）。

    speed>1 时每段先 atempo 提速：拍时长已被引擎按 speed 压缩、偏移也随之收紧（见 audio_segments），
    音频同步提速才放得进压缩后的拍、音画对齐。speed=1.0 时 atempo 串为空，行为与提速前完全一致。

    末尾 asetpts=N/SR/TB 按样本数重建单调 PTS：没有任何一段从 0 起（各拍都带前导偏移，见
    audio_segments 以开抓时刻为零点），amix 在 atempo 之后会吐出 NOPTS 初始 DTS、冲到 INT64_MAX
    把 muxer 撞崩（仅提速时触发）。asetpts 只重标时间戳、不动样本，前导静音与各段对齐都原样保留。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats"]
    for path, _, _ in segs:
        cmd += ["-i", str(path)]
    tempo = _atempo_chain(speed)
    parts = [f"[{i}:a]{tempo}adelay={int(off * 1000)}:all=1,volume={gain}[a{i}]"
             for i, (_, off, gain) in enumerate(segs)]
    labels = "".join(f"[a{i}]" for i in range(len(segs)))
    filt = (";".join(parts) + ";" + labels
            + f"amix=inputs={len(segs)}:normalize=0:dropout_transition=0,asetpts=N/SR/TB[aout]")
    cmd += ["-filter_complex", filt, "-map", "[aout]", str(out_audio)]
    return cmd


def ffmpeg_bgm_concat_cmd(tracks: list[Path], out: Path) -> list[str]:
    """把播放列表按固定顺序首尾相接拼成一条 bgm 床（opus）。顺序即 BGM_PLAYLIST——录屏循环
    铺这一条，所以录出来的背景乐可复现。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats"]
    for t in tracks:
        cmd += ["-i", str(t)]
    streams = "".join(f"[{i}:a]" for i in range(len(tracks)))
    cmd += ["-filter_complex", f"{streams}concat=n={len(tracks)}:v=0:a=1[bgm]",
            "-map", "[bgm]", "-c:a", "libopus", "-b:a", "96k", str(out)]
    return cmd


def ffmpeg_capture_cmd(
    display: str, width: int, height: int, fps: int, encoder: str, out_video: Path
) -> list[str]:
    """x11grab 实时抓 Xvfb 那块虚拟屏的裸帧 → 单趟编码出视频（边播边编，无中间 webm、不二次压缩）。

    encoder=vaapi 走核显硬编 HEVC（CQP qp18 + hvc1 标记，体积小、上传平台友好）；encoder=x264
    走软编 H.264 veryfast crf16（必须跟得上实时，软编 HEVC 在弱机上压不动 1080p30，故软编路径只给 264）。
    qp/crf 压到 18/16：舞台满屏文字（字幕/名片/HUD）对量化敏感，默认档会发糊，这俩值接近视觉无损。
    抓的是 :display 屏左上 width×height 区域，光标不入画（-draw_mouse 0）。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
           "-f", "x11grab", "-draw_mouse", "0",
           "-video_size", f"{width}x{height}", "-framerate", str(fps),
           "-i", f":{display}.0+0,0"]
    if encoder == "vaapi":
        cmd += ["-vaapi_device", RENDER_DEVICE, "-vf", "format=nv12,hwupload",
                "-c:v", "hevc_vaapi", "-rc_mode", "CQP", "-qp", "18", "-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p"]
    cmd += [str(out_video)]
    return cmd


def ffmpeg_mux_cmd(video: Path, audio: Path | None, bgm: Path | None, out: Path) -> list[str]:
    """把已编好的视频流 `-c:v copy` 原样拷进 mp4 + 叠音轨（不二次编码视频，瞬时）。

    视频在 capture 期间就已是成品 H.264/HEVC（x11grab 单趟编出），这里只换容器 + 拼音轨：
    人声/音效轨 audio（已含各拍语音+音效）、背景床 bgm 任意组合。bgm 用 -stream_loop -1 整段
    循环铺满、压在 BGM_VOLUME；两者都有则 amix 混在一起。都没有时只拷视频、不出音轨。

    输出长度恒等于视频长度（视频是权威时间轴）：音轨侧恒为无限长（bgm 循环 / apad 补静音），
    靠 -shortest 钳到视频末尾——人声短于视频不留空尾，--limit-seconds 中途截断也截到视频末尾。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats", "-i", str(video)]
    # 输入编号：0=视频，随后依次是 audio、bgm（哪个在场哪个才占一位）
    voice_idx = bgm_idx = None
    idx = 1
    if audio is not None:
        voice_idx = idx; cmd += ["-i", str(audio)]; idx += 1
    if bgm is not None:
        bgm_idx = idx; cmd += ["-stream_loop", "-1", "-i", str(bgm)]; idx += 1

    cmd += ["-map", "0:v:0", "-c:v", "copy"]

    # 音轨：人声 + 背景床按在场情况拼 filter_complex；都不在则无音轨。
    if voice_idx is not None and bgm_idx is not None:
        filt = f"[{bgm_idx}:a]volume={BGM_VOLUME}[bg];[{voice_idx}:a][bg]amix=inputs=2:normalize=0:dropout_transition=0[aout]"
    elif bgm_idx is not None:
        filt = f"[{bgm_idx}:a]volume={BGM_VOLUME}[aout]"
    elif voice_idx is not None:
        filt = f"[{voice_idx}:a]apad[aout]"  # 无 bgm：靠 apad 静音补齐到视频长
    else:
        filt = None
    if filt is not None:
        cmd += ["-filter_complex", filt, "-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += [str(out)]
    return cmd


# ---------------------------------------------------------------- 引擎探针 / 等待


def _tap_engine(engine) -> Timeline:
    """包住 engine._publish：每拍 emit 后顺手把时间线记下来（seq 去重，暂停/旋钮的重发不计）。
    终局拍另记下定格收尾挂钟时刻，供停录赶在幕间下发渲染前收手。"""
    timeline = Timeline()
    seen: set[int] = set()
    orig = engine._publish

    async def tap() -> None:
        feed = engine.scene.feed
        if feed and feed[-1].seq not in seen:
            fi = feed[-1]
            seen.add(fi.seq)
            cues = cues_for(fi.directive, fi.duration)
            timeline.beats.append(Beat(fi.seq, fi.started_at, getattr(fi.directive, "audio_url", None), cues))
            if isinstance(fi.directive, GameOver):
                timeline.gameover_ends_at = fi.started_at + fi.duration  # 定格演完 = 幕间即将下发的挂钟时刻
            elapsed = fi.started_at - timeline.beats[0].started_at  # 视频时间轴（以第一拍为零点）
            tag, color, detail = _beat_summary(fi.directive, engine.scene.seats)
            note = "[cyan]♪[/cyan]" if getattr(fi.directive, "audio_url", None) else " "
            # 每拍固定一行：超宽（中文台词）裁切而非换行，进度日志更整齐
            _console.print(f"[dim]{_mmss(elapsed)}[/dim] {note} [{color}]{tag}[/{color}] {detail}", no_wrap=True)
        await orig()

    engine._publish = tap
    return timeline


async def _wait(pred: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError(f"等 {what} 超时（{timeout}s）")
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------- 采集（起栈 + 抓屏）


def _free_display() -> int:
    """挑一个没被占的 X display 号（看 /tmp 下的锁文件 / 套接字）。"""
    for d in range(99, 160):
        if not Path(f"/tmp/.X{d}-lock").exists() and not Path(f"/tmp/.X11-unix/X{d}").exists():
            return d
    raise RuntimeError("找不到空闲 X display（:99~:159 都被占）")


def _stop_capture(proc: subprocess.Popen) -> None:
    """给 x11grab ffmpeg 发 SIGINT 优雅收尾（写完 moov、出完整可播 mp4）；逾时再硬杀兜底。"""
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


async def _capture(settings: RecordSettings, web_dist: Path, tmpdir: Path) -> tuple[list[Beat], float, Path]:
    """起一次性 stage + Xvfb 虚拟屏 + 带头 Chromium，x11grab 实时抓屏单趟编码。
    返回 (时间线, origin=开抓时刻=视频零点, 视频 mp4 路径)。"""
    # 引擎旋钮按 EngineKnobs 字段名机械转移（pace/speed/feed_window/prefetch_factor）——
    # 共享基类是唯一真源，新旋钮在那加一次这里自动带上，不再逐字段手抄（收分叉）。
    stage_settings = StageSettings(
        replay_root=settings.dir.parent,
        stage_token=secrets.token_urlsafe(8),
        console_token=secrets.token_urlsafe(8),
        web_dist=web_dist,
        loop_pool=False,  # 只放队列里这一局，放完即转幕间，绝不自动补别的局
        **{k: getattr(settings, k) for k in EngineKnobs.model_fields},
    )
    app = create_app(stage_settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    server_task = asyncio.create_task(server.serve())

    # 显示源在边界一次定下：默认起一块 Xvfb 虚拟屏（无头服务器照跑）；--headed 复用现有 $DISPLAY
    # （带屏机本地调试）。下游统一拿一个 display 号；xvfb 进程为 None 即"无需收尾"的空对象。
    if settings.headed:
        display = os.environ["DISPLAY"].lstrip(":").split(".")[0]
        xvfb: subprocess.Popen | None = None
    else:
        dnum = _free_display()
        xvfb = subprocess.Popen(
            ["Xvfb", f":{dnum}", "-screen", "0", f"{settings.width}x{settings.height}x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        display = str(dnum)

    try:
        await _wait(lambda: server.started, timeout=30, what="服务启动")
        if xvfb is not None:
            await _wait(lambda: Path(f"/tmp/.X11-unix/X{display}").exists(), timeout=10, what="Xvfb 启动")
        port = server.servers[0].sockets[0].getsockname()[1]
        engine = app.state.engine
        timeline = _tap_engine(engine)
        replay_id = settings.dir.name
        url = f"http://127.0.0.1:{port}/?token={stage_settings.stage_token}#/stage"

        async with async_playwright() as pw:
            # 用 persistent_context（单一窗口）而非 launch+new_context：后者会另开一个窗口，无窗管时
            # x11grab 在 0,0 抓到的是带浏览器外壳的那个默认窗，画面被标签栏/地址栏挤偏。单窗 + --kiosk
            # 才是无边框全幅（实测 launch+new_context 怎么 kiosk 都去不掉外壳，persistent+kiosk 干净）。
            profile = tmpdir / "chrome-profile"
            (profile / "Default").mkdir(parents=True, exist_ok=True)
            # 关掉"翻译此页"气泡：中文页会触发它、飘在右上角入画。命令行 --disable-features 会被
            # Playwright 自己的那份盖掉，所以落到 profile 偏好里（与 arg 顺序无关，最稳）。
            (profile / "Default" / "Preferences").write_text(json.dumps({"translate": {"enabled": False}}))
            context = await pw.chromium.launch_persistent_context(
                str(profile),
                headless=False,  # x11grab 抓的是真窗口：必须带头把画面渲染进 Xvfb 那块屏
                env={**os.environ, "DISPLAY": f":{display}"},
                no_viewport=True,  # 用真窗口尺寸（kiosk 占满 = 屏尺寸），不套 Playwright 默认视口
                ignore_default_args=["--enable-automation"],  # 去掉"自动化控制"信息条
                args=["--kiosk", "--window-position=0,0", f"--window-size={settings.width},{settings.height}",
                      "--force-device-scale-factor=1", "--hide-scrollbars", "--disable-infobars",
                      "--lang=zh-CN",  # UI 语言对齐中文页，进一步压住翻译提示
                      "--mute-audio", "--autoplay-policy=no-user-gesture-required"],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url)

            # 先入队、等本局真上屏、首帧画定，再开抓——待机/幕间一帧都挡在录制窗口外。
            engine.queue = [replay_id]  # 入队这一局，主循环 1s 内拾起
            await _wait(lambda: engine.phase == "playing", timeout=60, what="开局")
            await asyncio.sleep(_FIRST_FRAME_SETTLE)  # 让开局首帧渲染落定，开抓即开局而非待机

            # 开抓：x11grab 实时录这块屏 → 单趟编码出成品视频流。origin 即开抓时刻 = 视频零点。
            cap_out = tmpdir / "capture.mp4"
            cap_proc = subprocess.Popen(
                ffmpeg_capture_cmd(display, settings.width, settings.height, settings.fps, settings.encoder, cap_out),
                stdin=subprocess.DEVNULL,
            )
            origin = time.time()
            try:
                await asyncio.sleep(_CAPTURE_WARMUP)  # 让 x11grab 真挂上画面（画面已是开局静帧、不黑、不挪 origin）
                # 主停录信号：终局定格演完即停，赶在幕间下发渲染前收手；坏局/跳局没有终局拍时回落到
                # phase 离开 playing 的兜底；次级安全阀：到时长上限即停。
                cap = time.monotonic() + settings.limit_seconds if settings.limit_seconds else None

                def _reached_end() -> bool:
                    if timeline.gameover_ends_at is not None and time.time() >= timeline.gameover_ends_at:
                        return True
                    if engine.phase != "playing":
                        return True
                    return cap is not None and time.monotonic() >= cap

                await _wait(_reached_end, timeout=3600, what="终局")
            finally:
                _stop_capture(cap_proc)  # 无论正常终局还是异常，都把录制收成完整 mp4

            await context.close()  # persistent_context.close() 一并关掉浏览器

        return timeline.beats, origin, cap_out
    finally:
        server.should_exit = True
        await server_task
        if xvfb is not None:
            xvfb.terminate()
            try:
                xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xvfb.kill()


# ---------------------------------------------------------------- 编排


def _run(cmd: list[str], desc: str, cwd: Path | None = None) -> None:
    """跑一步外部命令（ffmpeg / npm）：进度直接继承终端实时刷出，不再吞掉。"""
    _console.print(f"[bold]{desc}[/bold]")
    subprocess.run(cmd, check=True, cwd=cwd)


def _build_web(tmpdir: Path) -> Path:
    """每次录制都现构建前端到临时目录，返回 dist 路径。

    强制重构建（约 3.5s）换来"永远录不到旧 web"——不再有 npm run build 漏跑的地雷，
    也没有 web/dist 这个易腐的常驻产物。产物落进本局的 tmpdir，录完随之清掉
    （bgm/sfx 的 .opus 也由 vite 从 web/public 一并拷进来，下游照常按 dist/bgm、dist/sfx 取）。"""
    dist = tmpdir / "web"
    _run(["npm", "run", "build", "--", "--outDir", str(dist), "--emptyOutDir"],
         "构建前端…", cwd=_WEB_SRC)
    return dist


def _build_bgm(settings: RecordSettings, web_dist: Path, tmpdir: Path) -> Path | None:
    """按 BGM_PLAYLIST 拼一条 bgm 床；--no-bgm、播放列表为空、或素材缺失/拼接失败都优雅跳过
    （只警告、不中断录制——背景乐是锦上添花，没了照样出片）。

    缺失常见于 clone 时没装 git-lfs：bgm/*.opus 会是 LFS 指针文本而非真音频，concat 会报错，
    这里兜住并提示。"""
    if not settings.bgm or not BGM_PLAYLIST:
        return None
    tracks = [web_dist / "bgm" / f"{n}.opus" for n in BGM_PLAYLIST]
    missing = [t for t in tracks if not t.is_file()]
    if missing:
        _console.print(f"[yellow]⚠ 缺 BGM 素材，跳过背景乐：{', '.join(m.name for m in missing)}（git-lfs 没拉？）[/yellow]")
        return None
    bed = tmpdir / "bgm.opus"
    try:
        _run(ffmpeg_bgm_concat_cmd(tracks, bed), f"拼接 BGM 床（{len(tracks)} 首循环）…")
    except subprocess.CalledProcessError:
        _console.print("[yellow]⚠ BGM 拼接失败，跳过背景乐（素材可能是 LFS 指针而非真音频）[/yellow]")
        return None
    return bed


async def _record(settings: RecordSettings) -> None:
    limit = f" · 上限 {settings.limit_seconds:g}s" if settings.limit_seconds else ""
    _console.rule(f"[bold]录制 {settings.dir.name}[/bold]")
    speed = f" · speed {settings.speed:g}×" if settings.speed != 1.0 else ""
    _console.print(f"[dim]→ {settings.out} · {settings.width}×{settings.height} · 编码 {settings.encoder} · pace {settings.pace:g}{speed}{limit}[/dim]")
    with tempfile.TemporaryDirectory(prefix="stage-record-") as tmp:
        tmpdir = Path(tmp)
        web_dist = _build_web(tmpdir)  # 每局现构建前端，永远录最新（无 --web-dist、无旧 dist 地雷）
        beats, origin, video = await _capture(settings, web_dist, tmpdir)  # 视频已是成品 H.264/HEVC（边播边编）
        segs = audio_segments(beats, settings.dir.parent, web_dist / "sfx", settings.audio_shift, origin)
        audio: Path | None = None
        if segs:
            audio = tmpdir / "audio.m4a"
            _run(ffmpeg_audio_cmd(segs, audio, settings.speed), f"拼接音轨（{len(segs)} 段语音/音效）…")
        bgm = _build_bgm(settings, web_dist, tmpdir)
        _run(ffmpeg_mux_cmd(video, audio, bgm, settings.out), "封装视频（视频流拷贝 + 音轨）…")
    size_mb = settings.out.stat().st_size / 1e6
    _console.print(f"[bold green]✓ 已生成[/bold green] {settings.out} [dim]({size_mb:.1f} MB)[/dim]")


def main(argv: list[str]) -> int:
    """`stage-record` 的薄入口：argv 全交给 RecordSettings 的 CLI source。

    配置错误（坏参数 / 校验失败）只打友好消息、不甩 traceback（与 games/cli.py 同口径）。"""
    try:
        settings = RecordSettings.load(cli_args=argv)
    except ValidationError as e:
        msg = "；".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        _console.print(f"[red]配置错误:[/red] {msg}")
        return 1
    asyncio.run(_record(settings))
    return 0


def main_cli() -> None:
    """console_script 零参入口：从 sys.argv 取参数转交 main。"""
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    main_cli()
