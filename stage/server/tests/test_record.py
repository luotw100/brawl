"""record 的纯函数单测：拍时间线→音频偏移折算 + ffmpeg 命令拼装（不跑 Playwright/ffmpeg）。"""

from __future__ import annotations

from pathlib import Path

from server.record import Beat, audio_segments, ffmpeg_audio_cmd, ffmpeg_mux_cmd

ROOT = Path("/replays-root")


def test_audio_segments_offsets_relative_to_first_beat():
    beats = [
        Beat(seq=1, started_at=100.0, audio_url="/replays/g/tts/a.opus"),
        Beat(seq=2, started_at=103.5, audio_url="/replays/g/tts/b.opus"),
    ]
    segs = audio_segments(beats, ROOT, shift=0.0)
    assert segs == [
        (ROOT / "g/tts/a.opus", 0.0),  # 第一拍即零点
        (ROOT / "g/tts/b.opus", 3.5),
    ]


def test_audio_segments_skips_silent_and_applies_shift_clamped():
    beats = [
        Beat(seq=1, started_at=100.0, audio_url=None),  # 无声拍跳过，但仍是零点
        Beat(seq=2, started_at=101.0, audio_url="/replays/g/tts/b.opus"),
    ]
    # shift=-2 把 b 的 1.0s 推到负，下钳到 0
    segs = audio_segments(beats, ROOT, shift=-2.0)
    assert segs == [(ROOT / "g/tts/b.opus", 0.0)]


def test_audio_segments_empty():
    assert audio_segments([], ROOT, shift=0.0) == []


def test_ffmpeg_audio_cmd_delays_and_mix():
    segs = [(Path("/x/a.opus"), 0.0), (Path("/x/b.opus"), 3.5)]
    cmd = ffmpeg_audio_cmd(segs, Path("/tmp/out.m4a"))
    assert cmd[:2] == ["ffmpeg", "-y"]
    assert "-stats" in cmd  # 进度透传，不再吞掉 ffmpeg 输出
    assert cmd.count("-i") == 2
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert "adelay=0:all=1[a0]" in filt
    assert "adelay=3500:all=1[a1]" in filt  # 3.5s → 3500ms
    assert "amix=inputs=2:normalize=0" in filt
    assert cmd[-1] == "/tmp/out.m4a"


def test_ffmpeg_mux_cmd_vaapi_with_audio():
    cmd = ffmpeg_mux_cmd(Path("/v.webm"), Path("/a.m4a"), lead=2.0, out=Path("/o.mp4"),
                         encoder="vaapi", fps=30)
    assert "-vaapi_device" in cmd
    assert "h264_vaapi" in cmd
    assert cmd[cmd.index("-ss") + 1] == "2.000"  # 裁前导留白
    assert "1:a:0" in cmd  # 叠了音轨
    assert "apad" in cmd and "-shortest" in cmd  # 输出长度恒等于视频：静音补齐 + 钳到视频末尾
    assert cmd[-1] == "/o.mp4"


def test_ffmpeg_mux_cmd_x264_no_audio():
    cmd = ffmpeg_mux_cmd(Path("/v.webm"), None, lead=0.0, out=Path("/o.mp4"),
                         encoder="x264", fps=24)
    assert "libx264" in cmd
    assert "-vaapi_device" not in cmd
    assert cmd.count("-i") == 1  # 没有音频输入
    assert "-shortest" not in cmd
    assert "1:a:0" not in cmd
    assert cmd[cmd.index("-r") + 1] == "24"
