"""record 的纯函数单测：拍时间线→音频偏移折算 + ffmpeg 命令拼装（不跑 Playwright/ffmpeg）。"""

from __future__ import annotations

from pathlib import Path

from server.bgm import BGM_VOLUME
from server.record import (
    Beat,
    audio_segments,
    ffmpeg_audio_cmd,
    ffmpeg_bgm_concat_cmd,
    ffmpeg_mux_cmd,
)
from server.sfx import SFX_VOLUME

ROOT = Path("/replays-root")
SFX = Path("/sfx-root")


def test_audio_segments_offsets_relative_to_first_beat():
    beats = [
        Beat(seq=1, started_at=100.0, audio_url="/replays/g/tts/a.opus"),
        Beat(seq=2, started_at=103.5, audio_url="/replays/g/tts/b.opus"),
    ]
    segs = audio_segments(beats, ROOT, SFX, shift=0.0)
    assert segs == [
        (ROOT / "g/tts/a.opus", 0.0, 1.0),  # 第一拍即零点，人声增益 1.0
        (ROOT / "g/tts/b.opus", 3.5, 1.0),
    ]


def test_audio_segments_skips_silent_and_applies_shift_clamped():
    beats = [
        Beat(seq=1, started_at=100.0, audio_url=None),  # 无声拍跳过，但仍是零点
        Beat(seq=2, started_at=101.0, audio_url="/replays/g/tts/b.opus"),
    ]
    # shift=-2 把 b 的 1.0s 推到负，下钳到 0
    segs = audio_segments(beats, ROOT, SFX, shift=-2.0)
    assert segs == [(ROOT / "g/tts/b.opus", 0.0, 1.0)]


def test_audio_segments_sfx_cues_offset_on_top_of_beat():
    # cue 的拍内偏移叠在拍起点上；音效走 sfx_dir + 压低增益
    beats = [
        Beat(seq=1, started_at=100.0, audio_url=None),  # 仍是零点
        Beat(seq=2, started_at=102.0, audio_url="/replays/g/tts/b.opus",
             cues=[("stamp", 0.0), ("stamp", 0.5), ("vote-pass", 1.0)]),
    ]
    segs = audio_segments(beats, ROOT, SFX, shift=0.0)
    assert segs == [
        (ROOT / "g/tts/b.opus", 2.0, 1.0),  # 人声在拍起点
        (SFX / "stamp.opus", 2.0, SFX_VOLUME),
        (SFX / "stamp.opus", 2.5, SFX_VOLUME),
        (SFX / "vote-pass.opus", 3.0, SFX_VOLUME),
    ]


def test_audio_segments_empty():
    assert audio_segments([], ROOT, SFX, shift=0.0) == []


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


def test_ffmpeg_mux_cmd_vaapi_with_audio_no_bgm():
    cmd = ffmpeg_mux_cmd(Path("/v.webm"), Path("/a.m4a"), None, lead=2.0, out=Path("/o.mp4"),
                         encoder="vaapi", fps=30)
    assert "-vaapi_device" in cmd
    assert "h264_vaapi" in cmd
    assert cmd[cmd.index("-ss") + 1] == "2.000"  # 裁前导留白
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert filt == "[1:a]apad[aout]"  # 无 bgm：人声靠 apad 静音补齐
    assert "-map" in cmd and "[aout]" in cmd and "-shortest" in cmd
    assert cmd[-1] == "/o.mp4"


def test_ffmpeg_mux_cmd_x264_no_audio_no_bgm():
    cmd = ffmpeg_mux_cmd(Path("/v.webm"), None, None, lead=0.0, out=Path("/o.mp4"),
                         encoder="x264", fps=24)
    assert "libx264" in cmd
    assert "-vaapi_device" not in cmd
    assert cmd.count("-i") == 1  # 只有视频输入
    assert "-shortest" not in cmd
    assert "-filter_complex" not in cmd  # 无音轨
    assert cmd[cmd.index("-r") + 1] == "24"


def test_ffmpeg_mux_cmd_audio_plus_bgm_mixes():
    cmd = ffmpeg_mux_cmd(Path("/v.webm"), Path("/a.m4a"), Path("/bgm.opus"), lead=1.0,
                         out=Path("/o.mp4"), encoder="x264", fps=30)
    # 输入：0=视频，1=人声，2=bgm（循环）
    assert cmd.count("-i") == 3
    assert "-stream_loop" in cmd and cmd[cmd.index("-stream_loop") + 1] == "-1"
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert filt == f"[2:a]volume={BGM_VOLUME}[bg];[1:a][bg]amix=inputs=2:normalize=0:dropout_transition=0[aout]"
    assert "-shortest" in cmd  # 循环 bgm 无限长，靠 -shortest 钳到视频末尾


def test_ffmpeg_mux_cmd_bgm_only():
    cmd = ffmpeg_mux_cmd(Path("/v.webm"), None, Path("/bgm.opus"), lead=0.0,
                         out=Path("/o.mp4"), encoder="x264", fps=30)
    assert cmd.count("-i") == 2  # 视频 + bgm
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
