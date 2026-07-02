"""PlayPlan DSL invariants: live and record share the same compiled tracks."""

from __future__ import annotations

from pathlib import Path

from server.engine import Engine
from server.games.avalon.protocol import LadyCheck, Speech
from server.playplan import compile_stage_beats, load_plan, plan_path
from server.protocol import HostSpeech
from server.record import Beat, audio_segments
from server.settings import StageSettings
from server.sfx import SFX_VOLUME


def test_plans_load_for_supported_games():
    assert load_plan(plan_path("avalon")).version == 1
    assert load_plan(plan_path("werewolf")).version == 1


def test_host_reveal_is_skipped_by_dsl():
    plan = load_plan(plan_path("avalon"))
    beats = compile_stage_beats([
        HostSpeech(name="司仪", beat="reveal", text="公布身份", audio_url="/h"),
    ], plan, {"/h": 10.0}, speed=1.0)

    assert beats == []


def test_lady_host_and_player_speech_are_separate_beats():
    plan = load_plan(plan_path("avalon"))
    host = HostSpeech(name="司仪", beat="lady", text="仙女环节", audio_url="/host")
    check = LadyCheck(
        holder_no=5,
        target_no=8,
        truth="good",
        choose_monologue="",
    )
    speech = Speech(phase="lady", seat_no=5, text="我查验八号，是蓝方。", monologue="", audio_url="/main")

    beats = compile_stage_beats(
        [host, check, speech],
        plan,
        {"/host": 4.0, "/main": 11.0},
        speed=1.0,
    )

    assert [b.directive.kind for b in beats] == ["lady_check", "speech"]
    assert beats[0].content_at == 4.2
    assert [(t.kind, t.audio_url, t.start) for t in beats[0].tracks if t.kind != "sfx"] == [
        ("narration", "/host", 0.0),
    ]
    assert [(t.kind, t.audio_url, t.start) for t in beats[1].tracks if t.kind != "sfx"] == [
        ("main", "/main", 0.0),
    ]
    assert beats[0].duration == 4.2
    assert beats[1].duration == 11.5


def test_record_audio_segments_use_compiled_tracks_without_reinterpretation():
    plan = load_plan(plan_path("avalon"))
    host = HostSpeech(name="司仪", beat="lady", text="仙女环节", audio_url="/replays/g/tts/host.opus")
    check = LadyCheck(
        holder_no=5,
        target_no=8,
        truth="good",
        choose_monologue="",
    )
    speech = Speech(
        phase="lady", seat_no=5, text="我查验八号，是蓝方。", monologue="",
        audio_url="/replays/g/tts/main.opus",
    )
    stage_beats = compile_stage_beats(
        [host, check, speech],
        plan,
        {"/replays/g/tts/host.opus": 4.0, "/replays/g/tts/main.opus": 11.0},
        speed=1.0,
    )
    host_beat, speech_beat = stage_beats

    segs = audio_segments(
        [
            Beat(seq=1, started_at=100.0, tracks=host_beat.tracks),
            Beat(seq=2, started_at=100.0 + host_beat.duration, tracks=speech_beat.tracks),
        ],
        replay_root=Path("/root"),
        sfx_dir=Path("/sfx"),
        shift=0.0,
        origin=99.0,
    )

    voices = [(path, round(start, 1), volume) for path, start, volume in segs if str(path).startswith("/root/")]
    assert voices == [
        (Path("/root/g/tts/host.opus"), 1.0, 1.0),
        (Path("/root/g/tts/main.opus"), 5.2, 1.0),
    ]


def test_sfx_tracks_are_in_playplan_prefetch_and_record_source(settings: StageSettings):
    engine = Engine(settings, publish=lambda: None)
    beat = engine._stage_beats([
        Speech(phase="discuss", seat_no=1, text="可以。", monologue="心里想。", audio_url="/voice"),
    ])[0]

    sfx = [track for track in beat.tracks if track.kind == "sfx"]
    assert [(track.audio_url, track.start, track.volume) for track in sfx] == [
        ("/sfx/think.opus", 0.0, SFX_VOLUME),
    ]
