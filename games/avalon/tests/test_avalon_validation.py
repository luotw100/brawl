"""版本化 Avalon replay meta 的边界测试。"""

from __future__ import annotations

import json
import random

import pytest

from avalon.registry import load_registry
from avalon.settings import AvalonSettings, SettingsError
from avalon.validation import (
    AvalonReplayMeta,
    ResumeRequest,
    load_replay,
    resolve_and_validate,
)


def _meta() -> AvalonReplayMeta:
    registry = load_registry()
    settings = AvalonSettings(players=5, seed=7, opencode_model="fallback/model")
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    return AvalonReplayMeta(
        started_at="2026-07-18T12:00:00+00:00",
        player=settings.player,
        concurrency=settings.concurrency,
        render=settings.render,
        seed=settings.seed,
        setup=setup,
        model_labels={},
    )


def _write_replay_at(replay, payload: dict) -> ResumeRequest:
    replay.mkdir()
    (replay / "sentences.jsonl").write_text("", encoding="utf-8")
    (replay / "meta.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return ResumeRequest(replay=replay)


def _write_replay(tmp_path, payload: dict) -> ResumeRequest:
    return _write_replay_at(tmp_path / "replay", payload)


def test_meta_freezes_all_prompt_model_and_aliyun_voice_inputs() -> None:
    meta = _meta()
    setup = meta.setup
    assert meta.seed == 7
    players = setup.players
    assert len(setup.tones) == players
    assert len(setup.strategy_systems) == players
    assert len(setup.opencode_models) == players
    assert len(setup.names) == players
    assert len(setup.voices) == players
    assert len(setup.voice_designs) == players
    dumped = setup.model_dump()
    assert "self_hosted_voices" not in dumped
    assert "host_self_hosted_voice" not in dumped


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw.update(game="werewolf"), "Avalon schema v1"),
        (lambda raw: raw.update(schema_version=2), "Avalon schema v1"),
        (lambda raw: raw["setup"]["tones"].pop(), "逐座位列表长度"),
        (lambda raw: raw["setup"]["opencode_models"].__setitem__(0, " provider/model "), "providerID/modelID"),
        (lambda raw: raw["setup"].update(first_leader=99), "first_leader"),
        (lambda raw: raw["setup"].update(unknown="debt"), "extra_forbidden"),
    ],
)
def test_resume_rejects_wrong_game_version_or_malformed_setup(
    tmp_path,
    mutation,
    message: str,
) -> None:
    raw = _meta().model_dump(mode="json")
    mutation(raw)
    request = _write_replay(tmp_path, raw)
    with pytest.raises(SettingsError, match=message):
        load_replay(request)


def test_resume_wraps_broken_json_as_settings_error(tmp_path) -> None:
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "sentences.jsonl").write_text("", encoding="utf-8")
    (replay / "meta.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(SettingsError, match="Avalon schema v1"):
        load_replay(ResumeRequest(replay=replay))


def test_resume_wraps_corrupt_sentence_log_as_settings_error(tmp_path) -> None:
    request = _write_replay(tmp_path, _meta().model_dump(mode="json"))
    (request.replay / "sentences.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(SettingsError, match="Sentence Runtime 日志"):
        load_replay(request)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("replay", "Sentence Runtime 日志"),
        ("sentences.jsonl", "Sentence Runtime 日志"),
        ("meta.json", "Avalon schema v1"),
    ],
)
def test_resume_rejects_symlinked_inputs(tmp_path, entry: str, message: str) -> None:
    request = _write_replay_at(
        tmp_path / "real-replay",
        _meta().model_dump(mode="json"),
    )
    if entry == "replay":
        linked_replay = tmp_path / "linked-replay"
        linked_replay.symlink_to(request.replay, target_is_directory=True)
        request = ResumeRequest(replay=linked_replay)
    else:
        path = request.replay / entry
        target = tmp_path / f"real-{entry}"
        path.rename(target)
        path.symlink_to(target)

    with pytest.raises(SettingsError, match=message):
        load_replay(request)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("replay", "Sentence Runtime 日志"),
        ("sentences.jsonl", "Sentence Runtime 日志"),
        ("meta.json", "Avalon schema v1"),
    ],
)
def test_resume_rejects_non_regular_inputs(tmp_path, entry: str, message: str) -> None:
    if entry == "replay":
        replay = tmp_path / "replay-file"
        replay.write_text("not a directory", encoding="utf-8")
        request = ResumeRequest(replay=replay)
    else:
        request = _write_replay(tmp_path, _meta().model_dump(mode="json"))
        path = request.replay / entry
        path.unlink()
        path.mkdir()

    with pytest.raises(SettingsError, match=message):
        load_replay(request)


def test_resume_wraps_missing_replay_as_settings_error(tmp_path) -> None:
    request = ResumeRequest(replay=tmp_path / "missing")
    with pytest.raises(SettingsError, match="Sentence Runtime 日志"):
        load_replay(request)
