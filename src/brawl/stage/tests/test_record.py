"""record 的纯函数单测：拍时间线→音频偏移折算 + ffmpeg 命令拼装（不跑 Playwright/ffmpeg）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from brawl.stage.bgm import BGM_VOLUME
from brawl.stage.media import ReplayMediaResolver
from brawl.stage.protocol import AudioTrack
from brawl.stage.record import (
    Beat,
    _atempo_chain,
    audio_segments,
    ffmpeg_audio_cmd,
    ffmpeg_bgm_concat_cmd,
    ffmpeg_capture_cmd,
    ffmpeg_mux_cmd,
    recorded_beats,
)
from brawl.stage.record_settings import RENDER_DEVICE, RecordSettings
from brawl.stage.sfx import SFX_VOLUME

SFX = Path("/sfx-root")


def track(kind: str, url: str, start: float, volume: float = 1.0) -> AudioTrack:
    return AudioTrack(kind=kind, audio_url=url, start=start, volume=volume)


def make_media(tmp_path: Path, files: list[str]) -> ReplayMediaResolver:
    """造一份带 manifest 的 replay 媒体面，返回其 resolver（URL 全部由它产出）。"""
    tts = tmp_path / "tts"
    tts.mkdir(exist_ok=True)
    manifest = {}
    for i, name in enumerate(files, start=1):
        (tts / name).write_bytes(b"x")
        manifest[f"{i}.2"] = {"file": name, "duration_ms": 1000}
    (tts / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return ReplayMediaResolver(tmp_path)


@pytest.fixture
def media(tmp_path: Path) -> ReplayMediaResolver:
    return make_media(tmp_path, ["a.opus", "b.opus", "main.opus", "host.opus", "open.opus", "deal.opus", "speech.opus"])


def test_record_settings_video_path_is_inside_replay_dir(tmp_path):
    replay = tmp_path / "game1"
    replay.mkdir()
    (replay / "sentences.jsonl").write_text("", encoding="utf-8")
    (replay / "meta.json").write_text("{}", encoding="utf-8")

    settings = RecordSettings.load([str(replay), "--encoder", "x264"])

    assert settings.video_path == replay / "video.mp4"


def test_record_settings_rejects_non_save_dir(tmp_path):
    replay = tmp_path / "game1"
    replay.mkdir()  # 缺存档两件套

    with pytest.raises(ValidationError, match="不是 replay 存档目录"):
        RecordSettings.load([str(replay), "--encoder", "x264"])


def test_audio_segments_offsets_relative_to_origin(media):
    # origin = 开抓时刻 = 视频零点；偏移 = started_at − origin
    beats = [
        Beat(seq=1, started_at=100.0, tracks=[track("main", "/replay/tts/a.opus", 0.0)]),
        Beat(seq=2, started_at=103.5, tracks=[track("main", "/replay/tts/b.opus", 0.0)]),
    ]
    segs = audio_segments(beats, media, SFX, origin=99.0)
    assert segs == [
        (media.audio_path("/replay/tts/a.opus"), 1.0, 1.0),  # 100−99
        (media.audio_path("/replay/tts/b.opus"), 4.5, 1.0),  # 103.5−99
    ]


def test_audio_segments_skips_silent_and_clamps_negative_offset(media):
    beats = [
        Beat(seq=1, started_at=100.0),  # 无声拍跳过
        Beat(seq=2, started_at=98.0, tracks=[track("main", "/replay/tts/b.opus", 0.0)]),
    ]
    # origin=100 → b 在 −2s，下钳到 0
    segs = audio_segments(beats, media, SFX, origin=100.0)
    assert segs == [(media.audio_path("/replay/tts/b.opus"), 0.0, 1.0)]


def test_audio_segments_sfx_tracks_offset_on_top_of_beat(media):
    # sfx track 的拍内偏移叠在拍起点上；音效走 sfx_dir + 压低增益
    beats = [
        Beat(seq=1, started_at=100.0),
        Beat(seq=2, started_at=102.0, tracks=[
            track("main", "/replay/tts/b.opus", 0.0),
            track("sfx", "/sfx/stamp.opus", 0.0, SFX_VOLUME),
            track("sfx", "/sfx/stamp.opus", 0.5, SFX_VOLUME),
            track("sfx", "/sfx/vote-pass.opus", 1.0, SFX_VOLUME),
        ]),
    ]
    segs = audio_segments(beats, media, SFX, origin=100.0)
    assert segs == [
        (media.audio_path("/replay/tts/b.opus"), 2.0, 1.0),  # 人声在拍起点（102−100）
        (SFX / "stamp.opus", 2.0, SFX_VOLUME),
        (SFX / "stamp.opus", 2.5, SFX_VOLUME),
        (SFX / "vote-pass.opus", 3.0, SFX_VOLUME),
    ]


def test_audio_segments_narrations_offset_inside_beat(media):
    beats = [
        Beat(
            seq=1,
            started_at=100.0,
            tracks=[
                track("main", "/replay/tts/main.opus", 0.0),
                track("narration", "/replay/tts/host.opus", 1.25),
            ],
        ),
    ]
    segs = audio_segments(beats, media, SFX, origin=98.5)
    assert segs == [
        (media.audio_path("/replay/tts/main.opus"), 1.5, 1.0),
        (media.audio_path("/replay/tts/host.opus"), 2.75, 1.0),
    ]


def test_audio_segments_empty(media):
    assert audio_segments([], media, SFX, origin=0.0) == []


def test_audio_segments_rejects_foreign_url(media):
    beats = [Beat(seq=1, started_at=100.0, tracks=[track("main", "/replay/tts/ghost.opus", 0.0)])]
    with pytest.raises(ValueError, match="不是本 replay 的媒体"):
        audio_segments(beats, media, SFX, origin=0.0)


def test_recorded_beats_drop_preroll_before_capture_origin(media):
    beats = [
        Beat(seq=1, started_at=99.0, tracks=[track("main", "/replay/tts/open.opus", 0.0)]),
        Beat(seq=2, started_at=100.0, tracks=[track("main", "/replay/tts/deal.opus", 0.0)]),
        Beat(seq=3, started_at=104.0, tracks=[track("main", "/replay/tts/speech.opus", 0.0)]),
    ]

    assert [b.seq for b in recorded_beats(beats, origin=100.0)] == [2, 3]


def test_ffmpeg_audio_cmd_delays_and_mix():
    segs = [(Path("/x/a.opus"), 0.0, 1.0), (Path("/x/b.opus"), 3.5, SFX_VOLUME)]
    cmd = ffmpeg_audio_cmd(segs, Path("/tmp/out.m4a"))
    assert cmd[:2] == ["ffmpeg", "-y"]
    assert "-stats" in cmd  # 进度透传，不再吞掉 ffmpeg 输出
    assert cmd.count("-i") == 2
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert "adelay=0:all=1,volume=1.0[a0]" in filt
    assert f"adelay=3500:all=1,volume={SFX_VOLUME}[a1]" in filt  # 3.5s → 3500ms，音效压低
    # amix 后接 asetpts 重建单调 PTS：无零起拍 + atempo 会让 amix 吐 NOPTS 撞崩 muxer
    assert "amix=inputs=2:normalize=0:dropout_transition=0,asetpts=N/SR/TB[aout]" in filt
    assert cmd[-1] == "/tmp/out.m4a"


def test_ffmpeg_audio_cmd_default_speed_has_no_atempo():
    # speed=1.0（默认）：行为与提速前完全一致，不掺 atempo
    segs = [(Path("/x/a.opus"), 0.0, 1.0)]
    filt = ffmpeg_audio_cmd(segs, Path("/tmp/out.m4a"))[
        ffmpeg_audio_cmd(segs, Path("/tmp/out.m4a")).index("-filter_complex") + 1]
    assert "atempo" not in filt
    assert "[0:a]adelay=0:all=1,volume=1.0[a0]" in filt


def test_ffmpeg_audio_cmd_speed_prepends_atempo_per_segment():
    segs = [(Path("/x/a.opus"), 0.0, 1.0), (Path("/x/b.opus"), 3.5, SFX_VOLUME)]
    cmd = ffmpeg_audio_cmd(segs, Path("/tmp/out.m4a"), speed=1.5)
    filt = cmd[cmd.index("-filter_complex") + 1]
    # 每段都先 atempo 再 adelay：音频提速贴合被压缩的拍
    assert "[0:a]atempo=1.5,adelay=0:all=1,volume=1.0[a0]" in filt
    assert f"[1:a]atempo=1.5,adelay=3500:all=1,volume={SFX_VOLUME}[a1]" in filt


def test_atempo_chain():
    assert _atempo_chain(1.0) == ""  # 不提速 → 空串（调用方据此省略）
    assert _atempo_chain(1.5) == "atempo=1.5,"
    assert _atempo_chain(2.0) == "atempo=2,"
    assert _atempo_chain(2.5) == "atempo=2,atempo=1.25,"  # 越界拆连乘
    assert _atempo_chain(0.4) == "atempo=0.5,atempo=0.8,"


def test_ffmpeg_capture_cmd_vaapi_is_single_pass_hevc():
    cmd = ffmpeg_capture_cmd("99", 1920, 1080, 30, "vaapi", Path("/tmp/cap.mp4"))
    assert "x11grab" in cmd
    assert cmd[cmd.index("-i") + 1] == ":99.0+0,0"  # 抓 :99 屏左上 0,0
    assert cmd[cmd.index("-video_size") + 1] == "1920x1080"
    assert cmd[cmd.index("-framerate") + 1] == "30"
    assert cmd[cmd.index("-draw_mouse") + 1] == "0"  # 光标不入画
    assert "-vaapi_device" in cmd and RENDER_DEVICE in cmd
    assert "hevc_vaapi" in cmd  # 单趟硬编 HEVC
    assert cmd[cmd.index("-tag:v") + 1] == "hvc1"  # mp4 里的 HEVC 要 hvc1 标记
    assert cmd[-1] == "/tmp/cap.mp4"


def test_ffmpeg_capture_cmd_progress_is_opt_in():
    cmd = ffmpeg_capture_cmd("99", 1920, 1080, 30, "x264", Path("/tmp/cap.mp4"), progress=True)
    assert cmd[-3:] == ["-progress", "pipe:1", "/tmp/cap.mp4"]


def test_ffmpeg_capture_cmd_x264_software_realtime_fallback():
    cmd = ffmpeg_capture_cmd("0", 1280, 720, 24, "x264", Path("/tmp/cap.mp4"))
    assert "x11grab" in cmd
    assert cmd[cmd.index("-i") + 1] == ":0.0+0,0"
    assert "libx264" in cmd
    assert cmd[cmd.index("-preset") + 1] == "veryfast"  # 软编必须跟得上实时
    assert "-vaapi_device" not in cmd
    assert "hevc_vaapi" not in cmd  # 软编 HEVC 压不动 1080p30，软编路径只给 264


def test_ffmpeg_mux_cmd_copies_video_with_audio():
    cmd = ffmpeg_mux_cmd(Path("/v.mp4"), Path("/a.m4a"), None, out=Path("/o.mp4"))
    assert cmd.count("-i") == 2  # 视频 + 人声
    assert cmd[cmd.index("-c:v") + 1] == "copy"  # 视频流拷贝、不二次编码
    assert "-ss" not in cmd  # 不再裁前导（origin 已是视频零点，音频侧对齐）
    assert "libx264" not in cmd and "h264_vaapi" not in cmd and "-r" not in cmd
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert filt == "[1:a]apad[aout]"  # 无 bgm：人声靠 apad 静音补齐
    assert "-map" in cmd and "[aout]" in cmd and "-shortest" in cmd
    assert cmd[-1] == "/o.mp4"


def test_ffmpeg_mux_cmd_no_audio_no_bgm_just_copies():
    cmd = ffmpeg_mux_cmd(Path("/v.mp4"), None, None, out=Path("/o.mp4"))
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd.count("-i") == 1  # 只有视频输入
    assert "-shortest" not in cmd
    assert "-filter_complex" not in cmd  # 无音轨
    assert cmd[-1] == "/o.mp4"


def test_ffmpeg_mux_cmd_audio_plus_bgm_mixes():
    cmd = ffmpeg_mux_cmd(Path("/v.mp4"), Path("/a.m4a"), Path("/bgm.opus"), out=Path("/o.mp4"))
    # 输入：0=视频，1=人声，2=bgm（循环）
    assert cmd.count("-i") == 3
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-stream_loop" in cmd and cmd[cmd.index("-stream_loop") + 1] == "-1"
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert filt == f"[2:a]volume={BGM_VOLUME}[bg];[1:a][bg]amix=inputs=2:normalize=0:dropout_transition=0[aout]"
    assert "-shortest" in cmd  # 循环 bgm 无限长，靠 -shortest 钳到视频末尾


def test_ffmpeg_mux_cmd_bgm_only():
    cmd = ffmpeg_mux_cmd(Path("/v.mp4"), None, Path("/bgm.opus"), out=Path("/o.mp4"))
    assert cmd.count("-i") == 2  # 视频 + bgm
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-stream_loop" in cmd
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert filt == f"[1:a]volume={BGM_VOLUME}[aout]"
    assert "-shortest" in cmd


def test_ffmpeg_bgm_concat_cmd_order_preserved():
    tracks = [Path("/bgm/crusade.opus"), Path("/bgm/angevin.opus"), Path("/bgm/x.opus")]
    cmd = ffmpeg_bgm_concat_cmd(tracks, Path("/tmp/bgm.opus"))
    assert cmd.count("-i") == 3
    # 输入顺序即播放顺序
    assert cmd[cmd.index("-i") + 1] == "/bgm/crusade.opus"
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert filt == "[0:a][1:a][2:a]concat=n=3:v=0:a=1[bgm]"
    assert "libopus" in cmd
    assert cmd[-1] == "/tmp/bgm.opus"
