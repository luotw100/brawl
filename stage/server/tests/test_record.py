"""record 的纯函数单测：拍时间线→音频偏移折算 + ffmpeg 命令拼装（不跑 Playwright/ffmpeg）。"""

from __future__ import annotations

from pathlib import Path

from server.bgm import BGM_VOLUME
from server.record import (
    Beat,
    _atempo_chain,
    audio_segments,
    ffmpeg_audio_cmd,
    ffmpeg_bgm_concat_cmd,
    ffmpeg_capture_cmd,
    ffmpeg_mux_cmd,
)
from server.record_settings import RENDER_DEVICE
from server.sfx import SFX_VOLUME

ROOT = Path("/replays-root")
SFX = Path("/sfx-root")


def test_audio_segments_offsets_relative_to_origin():
    # origin = 开抓时刻 = 视频零点；偏移 = started_at − origin
    beats = [
        Beat(seq=1, started_at=100.0, audio_url="/replays/g/tts/a.opus"),
        Beat(seq=2, started_at=103.5, audio_url="/replays/g/tts/b.opus"),
    ]
    segs = audio_segments(beats, ROOT, SFX, shift=0.0, origin=99.0)
    assert segs == [
        (ROOT / "g/tts/a.opus", 1.0, 1.0),  # 100−99
        (ROOT / "g/tts/b.opus", 4.5, 1.0),  # 103.5−99
    ]


def test_audio_segments_skips_silent_and_applies_shift_clamped():
    beats = [
        Beat(seq=1, started_at=100.0, audio_url=None),  # 无声拍跳过
        Beat(seq=2, started_at=101.0, audio_url="/replays/g/tts/b.opus"),
    ]
    # origin=100 → b 在 1.0s；shift=-2 推到负，下钳到 0
    segs = audio_segments(beats, ROOT, SFX, shift=-2.0, origin=100.0)
    assert segs == [(ROOT / "g/tts/b.opus", 0.0, 1.0)]


def test_audio_segments_sfx_cues_offset_on_top_of_beat():
    # cue 的拍内偏移叠在拍起点上；音效走 sfx_dir + 压低增益
    beats = [
        Beat(seq=1, started_at=100.0, audio_url=None),
        Beat(seq=2, started_at=102.0, audio_url="/replays/g/tts/b.opus",
             cues=[("stamp", 0.0), ("stamp", 0.5), ("vote-pass", 1.0)]),
    ]
    segs = audio_segments(beats, ROOT, SFX, shift=0.0, origin=100.0)
    assert segs == [
        (ROOT / "g/tts/b.opus", 2.0, 1.0),  # 人声在拍起点（102−100）
        (SFX / "stamp.opus", 2.0, SFX_VOLUME),
        (SFX / "stamp.opus", 2.5, SFX_VOLUME),
        (SFX / "vote-pass.opus", 3.0, SFX_VOLUME),
    ]


def test_audio_segments_empty():
    assert audio_segments([], ROOT, SFX, shift=0.0, origin=0.0) == []


def test_ffmpeg_audio_cmd_delays_and_mix():
    segs = [(Path("/x/a.opus"), 0.0, 1.0), (Path("/x/b.opus"), 3.5, SFX_VOLUME)]
    cmd = ffmpeg_audio_cmd(segs, Path("/tmp/out.m4a"))
    assert cmd[:2] == ["ffmpeg", "-y"]
    assert "-stats" in cmd  # 进度透传，不再吞掉 ffmpeg 输出
    assert cmd.count("-i") == 2
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert "adelay=0:all=1,volume=1.0[a0]" in filt
    assert f"adelay=3500:all=1,volume={SFX_VOLUME}[a1]" in filt  # 3.5s → 3500ms，音效压低
    assert "amix=inputs=2:normalize=0" in filt
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
