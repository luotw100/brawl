"""Werewolf production assembly and replay boundary tests."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from brawl.games.run import RunSettings, SettingsError
from brawl.games.werewolf import run as werewolf_run
from brawl.games.werewolf.registry import load_registry
from brawl.games.werewolf.rules import DEFAULT_BOARD
from brawl.games.werewolf.validation import (
    build_replay_meta,
    load_replay_meta,
    resolve_and_validate,
)


def _meta_dict(tmp_path) -> dict[str, Any]:
    registry = load_registry()
    settings = RunSettings(board=DEFAULT_BOARD, replay=tmp_path / "fresh", seed=7, backend="random", render=False)
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    return build_replay_meta(
        settings,
        registry,
        setup,
        started_at=datetime(2026, 7, 18, tzinfo=UTC),
    ).model_dump(mode="json")


def _write_replay_envelope(run_dir: Path, meta: dict[str, Any] | str) -> None:
    run_dir.mkdir()
    (run_dir / "sentences.jsonl").write_text("", encoding="utf-8")
    raw = meta if isinstance(meta, str) else json.dumps(meta, ensure_ascii=False)
    (run_dir / "meta.json").write_text(raw, encoding="utf-8")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda meta: meta.update(game="avalon"), "game"),
        (lambda meta: meta.update(schema_version=2), "schema_version"),
        (lambda meta: meta["setup"].update(board="unknown"), "board"),
        (lambda meta: meta["setup"]["names"].pop(), "列表长度"),
        (lambda meta: meta["setup"]["roles"].__setitem__(0, "seer"), "角色池"),
        (lambda meta: meta["setup"]["voices"].__setitem__(0, " "), "string_too_short"),
        (lambda meta: meta.update(started_at="2026-07-18T00:00:00"), "时区"),
        (lambda meta: meta.update(board_name="错板子"), "board_name"),
        (lambda meta: meta["setup"].update(unexpected=True), "extra_forbidden"),
    ],
)
def test_resume_meta_rejects_wrong_game_version_shape_and_board(
    tmp_path,
    mutation,
    message,
) -> None:
    meta = _meta_dict(tmp_path)
    mutation(meta)
    run_dir = tmp_path / "replay"
    _write_replay_envelope(run_dir, meta)

    with pytest.raises(SettingsError, match=message):
        load_replay_meta(run_dir)


def test_resume_meta_wraps_malformed_json_as_settings_error(tmp_path) -> None:
    run_dir = tmp_path / "replay"
    _write_replay_envelope(run_dir, "{bad json")

    with pytest.raises(SettingsError, match="Werewolf replay meta"):
        load_replay_meta(run_dir)


def test_resume_wraps_corrupt_sentence_log_as_settings_error(tmp_path) -> None:
    run_dir = tmp_path / "replay"
    _write_replay_envelope(run_dir, _meta_dict(tmp_path))
    (run_dir / "sentences.jsonl").write_text("{}", encoding="utf-8")

    with pytest.raises(SettingsError, match="Sentence Runtime 日志"):
        load_replay_meta(run_dir)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("replay", "Sentence Runtime 日志"),
        ("sentences.jsonl", "Sentence Runtime 日志"),
        ("meta.json", "Werewolf replay meta"),
    ],
)
def test_resume_rejects_symlinked_inputs(tmp_path, entry: str, message: str) -> None:
    real_replay = tmp_path / "real-replay"
    _write_replay_envelope(real_replay, _meta_dict(tmp_path))
    if entry == "replay":
        replay = tmp_path / "linked-replay"
        replay.symlink_to(real_replay, target_is_directory=True)
    else:
        replay = real_replay
        path = replay / entry
        target = tmp_path / f"real-{entry}"
        path.rename(target)
        path.symlink_to(target)

    with pytest.raises(SettingsError, match=message):
        load_replay_meta(replay)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("replay", "Sentence Runtime 日志"),
        ("sentences.jsonl", "Sentence Runtime 日志"),
        ("meta.json", "Werewolf replay meta"),
    ],
)
def test_resume_rejects_non_regular_inputs(tmp_path, entry: str, message: str) -> None:
    if entry == "replay":
        replay = tmp_path / "replay-file"
        replay.write_text("not a directory", encoding="utf-8")
    else:
        replay = tmp_path / "replay"
        _write_replay_envelope(replay, _meta_dict(tmp_path))
        path = replay / entry
        path.unlink()
        path.mkdir()

    with pytest.raises(SettingsError, match=message):
        load_replay_meta(replay)


def test_resume_wraps_missing_replay_as_settings_error(tmp_path) -> None:
    with pytest.raises(SettingsError, match="Sentence Runtime 日志"):
        load_replay_meta(tmp_path / "missing")


def test_meta_freezes_every_prompt_model_and_aliyun_voice_input(tmp_path) -> None:
    registry = load_registry()
    settings = RunSettings(board=DEFAULT_BOARD, replay=tmp_path / "fresh", seed=11, backend="random", render=False)
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))

    assert setup.tones == tuple(registry.personas[pid].tone for pid in setup.cast)
    assert setup.strategy_systems == tuple(
        registry.strategies[sid].system for sid in setup.strategies
    )
    assert setup.opencode_models == tuple(
        registry.models[name].opencode_model or "" for name in setup.models
    )
    assert setup.names == tuple(registry.personas[pid].name for pid in setup.cast)
    assert setup.voices == tuple(registry.personas[pid].voice_id for pid in setup.cast)
    assert setup.voice_designs == tuple(
        registry.personas[pid].voice_design for pid in setup.cast
    )
    assert "self_hosted_voices" not in setup.model_dump()


@pytest.mark.asyncio
async def test_production_run_game_writes_versioned_meta_and_resume_restores(
    tmp_path,
) -> None:
    replay = tmp_path / "replay"
    settings = RunSettings(
        board=DEFAULT_BOARD,
        seed=19,
        backend="random",
        replay=replay,
        render=False,
    )

    final = await werewolf_run.run_game(settings)
    meta = load_replay_meta(replay)
    restored = await werewolf_run.resume_game(replay)

    assert meta.schema_version == 1
    assert meta.game == "werewolf"
    assert meta.backend == "random"
    assert restored.model_dump_json() == final.model_dump_json()


@pytest.mark.asyncio
async def test_production_resume_continues_without_live_registry(
    tmp_path,
    monkeypatch,
) -> None:
    replay = tmp_path / "complete"
    final = await werewolf_run.run_game(
        RunSettings(
            board=DEFAULT_BOARD,
            seed=23,
            backend="random",
            replay=replay,
            render=False,
        )
    )
    original_lines = (replay / "sentences.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in original_lines]
    middle_commit = rows[len(rows) // 2]["commit_id"]
    cut = next(
        index
        for index, row in enumerate(rows)
        if row["commit_id"] == middle_commit
    )
    assert cut > 0

    interrupted = tmp_path / "interrupted"
    interrupted.mkdir()
    (interrupted / "meta.json").write_text(
        (replay / "meta.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (interrupted / "sentences.jsonl").write_text(
        "\n".join(original_lines[:cut]) + "\n",
        encoding="utf-8",
    )

    def registry_must_not_be_loaded() -> None:
        raise AssertionError("resume 不得加载活 registry/YAML")

    monkeypatch.setattr(werewolf_run, "load_registry", registry_must_not_be_loaded)
    resumed = await werewolf_run.resume_game(interrupted)

    resumed_lines = (interrupted / "sentences.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert resumed.model_dump_json() == final.model_dump_json()
    assert resumed_lines == original_lines
