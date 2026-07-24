"""PlayPlan DSL invariants: 直播页和录制消费同一份编译轨道。"""

from __future__ import annotations

from pathlib import Path

from brawl.games.avalon.protocol import LadyCheck, LadyChoose, Speech
from brawl.games.avalon.stagecast import STAGE_GAME as AVALON
from brawl.games.werewolf.stagecast import STAGE_GAME as WEREWOLF
from brawl.stage.media import ReplayMediaResolver
from brawl.stage.playplan import compile_stage_beats, load_plan
from brawl.stage.protocol import HostSpeech
from brawl.stage.record import Beat, audio_segments
from brawl.stage.sfx import SFX_VOLUME


def test_plans_load_for_registered_games():
    assert load_plan(AVALON.plan_path).version == 1
    assert load_plan(WEREWOLF.plan_path).version == 1


def test_host_reveal_is_skipped_by_dsl():
    plan = load_plan(AVALON.plan_path)
    beats = compile_stage_beats([
        HostSpeech(name="司仪", beat="reveal", text="公布身份", audio_url="/h"),
    ], plan, {"/h": 10.0}, speed=1.0)

    assert beats == []


def test_lady_host_and_player_speech_are_separate_beats():
    plan = load_plan(AVALON.plan_path)
    host = HostSpeech(name="司仪", beat="lady", text="仙女环节", audio_url="/host")
    choose = LadyChoose(holder_no=5, target_no=8, choose_monologue="")
    check = LadyCheck(holder_no=5, target_no=8, truth="good")
    speech = Speech(phase="lady", seat_no=5, text="我查验八号，是蓝方。", monologue="", audio_url="/main")

    beats = compile_stage_beats(
        [host, choose, check, speech],
        plan,
        {"/host": 4.0, "/main": 11.0},
        speed=1.0,
    )

    assert [b.directive.kind for b in beats] == ["lady_choose", "lady_check", "speech"]
    assert beats[0].content_at == 4.2
    assert [(t.kind, t.audio_url, t.start) for t in beats[0].tracks if t.kind != "sfx"] == [
        ("narration", "/host", 0.0),
    ]
    assert [(t.kind, t.audio_url, t.start) for t in beats[2].tracks if t.kind != "sfx"] == [
        ("main", "/main", 0.0),
    ]
    assert beats[0].duration == 6.7
    assert beats[1].duration == 3.2
    assert beats[2].duration == 11.5


def test_record_audio_segments_use_compiled_tracks_without_reinterpretation(tmp_path):
    plan = load_plan(AVALON.plan_path)
    tts = tmp_path / "tts"
    tts.mkdir()
    import json

    (tts / "manifest.json").write_text(json.dumps({
        "1.host": {"file": "host.opus", "duration_ms": 4000},
        "2.5": {"file": "main.opus", "duration_ms": 11000},
    }), encoding="utf-8")
    media = ReplayMediaResolver(tmp_path)
    host = HostSpeech(name="司仪", beat="lady", text="仙女环节", audio_url="/replay/tts/host.opus")
    choose = LadyChoose(holder_no=5, target_no=8, choose_monologue="")
    check = LadyCheck(holder_no=5, target_no=8, truth="good")
    speech = Speech(
        phase="lady", seat_no=5, text="我查验八号，是蓝方。", monologue="",
        audio_url="/replay/tts/main.opus",
    )
    stage_beats = compile_stage_beats(
        [host, choose, check, speech],
        plan,
        media.durations(),
        speed=1.0,
    )
    host_beat, check_beat, speech_beat = stage_beats

    segs = audio_segments(
        [
            Beat(seq=1, started_at=100.0, tracks=host_beat.tracks),
            Beat(seq=2, started_at=100.0 + host_beat.duration, tracks=check_beat.tracks),
            Beat(seq=3, started_at=100.0 + host_beat.duration + check_beat.duration, tracks=speech_beat.tracks),
        ],
        media=media,
        sfx_dir=Path("/sfx"),
        origin=99.0,
    )

    voices = [(path, round(start, 1), volume) for path, start, volume in segs if str(path).startswith(str(tmp_path))]
    assert voices == [
        (media.audio_path("/replay/tts/host.opus"), 1.0, 1.0),
        (media.audio_path("/replay/tts/main.opus"), 10.9, 1.0),
    ]


def test_sfx_tracks_come_from_plan_cues():
    plan = load_plan(AVALON.plan_path)
    beat = compile_stage_beats(
        [Speech(phase="discuss", seat_no=1, text="可以。", monologue="心里想。", audio_url="/voice")],
        plan, {}, speed=1.0,
    )[0]

    sfx = [track for track in beat.tracks if track.kind == "sfx"]
    assert [(track.audio_url, track.start, track.volume) for track in sfx] == [
        ("/sfx/think.opus", 0.0, SFX_VOLUME),
    ]
