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
import secrets
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

_TAIL_SECONDS = 1.0  # 终局定格后多录一点，保证最后一帧落进视频文件

_console = Console()  # 进度输出：录制节拍 + ffmpeg 步骤（与捕获/编码同一条命令行）


@dataclass
class Beat:
    """一拍的录制时间线条目：seq 去重，started_at 定位偏移，audio_url 指向待拼的人声，
    cues 是这拍要叠的音效（名字 + 拍内偏移秒，tap 时由 cues_for 算好）。"""

    seq: int
    started_at: float
    audio_url: str | None
    cues: list[tuple[str, float]] = field(default_factory=list)


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
    beats: list[Beat], replay_root: Path, sfx_dir: Path, shift: float
) -> list[tuple[Path, float, float]]:
    """各拍 → (opus 文件路径, 在视频里的起始秒, 增益)。人声增益 1.0；音效压在 SFX_VOLUME。
    以第一拍为零点；偏移加 shift 后下钳到 0。音效偏移在拍起点上再加 cue 的拍内偏移。

    audio_url 形如 /replays/<id>/tts/<file>，剥掉 /replays/ 前缀即 replay_root 下的相对路径
    （与 app.py 的 get_audio 静态路由同一套构造，反过来落回文件系统）；音效在 sfx_dir/<name>.opus。"""
    if not beats:
        return []
    t0 = beats[0].started_at
    segs: list[tuple[Path, float, float]] = []
    for b in beats:
        base = b.started_at - t0 + shift
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
    音频同步提速才放得进压缩后的拍、音画对齐。speed=1.0 时 atempo 串为空，行为与提速前完全一致。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats"]
    for path, _, _ in segs:
        cmd += ["-i", str(path)]
    tempo = _atempo_chain(speed)
    parts = [f"[{i}:a]{tempo}adelay={int(off * 1000)}:all=1,volume={gain}[a{i}]"
             for i, (_, off, gain) in enumerate(segs)]
    labels = "".join(f"[a{i}]" for i in range(len(segs)))
    filt = ";".join(parts) + ";" + labels + f"amix=inputs={len(segs)}:normalize=0:dropout_transition=0[aout]"
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


def ffmpeg_mux_cmd(
    video: Path, audio: Path | None, bgm: Path | None, lead: float, out: Path, encoder: str, fps: int
) -> list[str]:
    """裁掉视频前 lead 秒（对齐到第一拍）+ 叠音轨 + 转码成 mp4。

    三种音源任意组合：人声/音效轨 audio（已含各拍语音+音效）、背景床 bgm。bgm 用 -stream_loop
    -1 整段循环铺满，压在 BGM_VOLUME；两者都有则 amix 混在一起。都没有（audio 与 bgm 皆 None）
    时只转视频、不出音轨。

    输出长度恒等于视频长度（视频是权威时间轴）：-shortest 钳到视频末尾。bgm 在场时它循环到
    无限长，正好托住人声短于视频的尾巴（替代旧的 apad 静音补齐）；--limit-seconds 中途截断时
    同样截到视频末尾。"""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats"]
    if encoder == "vaapi":
        cmd += ["-vaapi_device", RENDER_DEVICE]
    cmd += ["-ss", f"{lead:.3f}", "-i", str(video)]
    # 输入编号：0=视频，随后依次是 audio、bgm（哪个在场哪个才占一位）
    voice_idx = bgm_idx = None
    idx = 1
    if audio is not None:
        voice_idx = idx; cmd += ["-i", str(audio)]; idx += 1
    if bgm is not None:
        bgm_idx = idx; cmd += ["-stream_loop", "-1", "-i", str(bgm)]; idx += 1

    if encoder == "vaapi":
        cmd += ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-b:v", "6M"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"]
    cmd += ["-r", str(fps), "-map", "0:v:0"]

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
            cues = cues_for(fi.directive, fi.duration)
            beats.append(Beat(fi.seq, fi.started_at, getattr(fi.directive, "audio_url", None), cues))
            elapsed = fi.started_at - beats[0].started_at  # 视频时间轴（以第一拍为零点）
            tag, color, detail = _beat_summary(fi.directive, engine.scene.seats)
            note = "[cyan]♪[/cyan]" if getattr(fi.directive, "audio_url", None) else " "
            # 每拍固定一行：超宽（中文台词）裁切而非换行，进度日志更整齐
            _console.print(f"[dim]{_mmss(elapsed)}[/dim] {note} [{color}]{tag}[/{color}] {detail}", no_wrap=True)
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
    # 引擎旋钮按 EngineKnobs 字段名机械转移（pace/speed/feed_window/prefetch_factor）——
    # 共享基类是唯一真源，新旋钮在那加一次这里自动带上，不再逐字段手抄（收分叉）。
    stage_settings = StageSettings(
        replay_root=settings.dir.parent,
        stage_token=secrets.token_urlsafe(8),
        console_token=secrets.token_urlsafe(8),
        web_dist=settings.web_dist,
        loop_pool=False,  # 只放队列里这一局，放完即转幕间，绝不自动补别的局
        **{k: getattr(settings, k) for k in EngineKnobs.model_fields},
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
            viewport = {"width": settings.width, "height": settings.height}  # 默认 16:9 横屏（见 RecordSettings.width/height）
            context = await browser.new_context(
                viewport=viewport,
                record_video_dir=str(video_dir),
                record_video_size=viewport,
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


def _run(cmd: list[str], desc: str) -> None:
    """跑一步 ffmpeg：进度（-stats）直接继承终端实时刷出，不再吞掉。"""
    _console.print(f"[bold]{desc}[/bold]")
    subprocess.run(cmd, check=True)


def _build_bgm(settings: RecordSettings, tmpdir: Path) -> Path | None:
    """按 BGM_PLAYLIST 拼一条 bgm 床；--no-bgm、播放列表为空、或素材缺失/拼接失败都优雅跳过
    （只警告、不中断录制——背景乐是锦上添花，没了照样出片）。

    缺失常见于 clone 时没装 git-lfs：bgm/*.opus 会是 LFS 指针文本而非真音频，concat 会报错，
    这里兜住并提示。"""
    if not settings.bgm or not BGM_PLAYLIST:
        return None
    tracks = [settings.web_dist / "bgm" / f"{n}.opus" for n in BGM_PLAYLIST]
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
        beats, lead, video = await _capture(settings, tmpdir)
        segs = audio_segments(beats, settings.dir.parent, settings.web_dist / "sfx", settings.audio_shift)
        audio: Path | None = None
        if segs:
            audio = tmpdir / "audio.m4a"
            _run(ffmpeg_audio_cmd(segs, audio, settings.speed), f"拼接音轨（{len(segs)} 段语音/音效）…")
        bgm = _build_bgm(settings, tmpdir)
        _run(ffmpeg_mux_cmd(video, audio, bgm, lead, settings.out, settings.encoder, settings.fps), "编码视频…")
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
