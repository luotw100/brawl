"""AvalonRules 通过 production run/resume 装配的整局测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from avalon import run as avalon_run
from avalon.run import resume_game, run_game
from avalon.settings import AvalonSettings
from avalon.state import Role, Side
from avalon.validation import ResumeRequest, load_replay
from core.runtime.errors import FatalActorError


def _settings(replay: Path, **over: Any) -> AvalonSettings:
    return AvalonSettings(
        players=over.pop("players", 5),
        seed=over.pop("seed", 42),
        player="random",
        replay=replay,
        render=False,
        **over,
    )


@pytest.mark.asyncio
async def test_production_random_game_finishes_and_terminal_resume_is_identical(tmp_path) -> None:
    replay = tmp_path / "game"
    final = await run_game(_settings(replay))

    assert final.phase == "done"
    assert final.winner in ("good", "evil")
    assert final.win_reason in ("quests", "rejections", "assassination")

    meta = load_replay(ResumeRequest(replay=replay))
    assert meta.schema_version == 1
    assert meta.game == "avalon"
    assert meta.player == "random"
    before = (replay / "sentences.jsonl").read_bytes()
    restored = await resume_game(ResumeRequest(replay=replay))
    assert restored.model_dump_json() == final.model_dump_json()
    assert (replay / "sentences.jsonl").read_bytes() == before


@pytest.mark.asyncio
async def test_resume_never_reads_live_registry(tmp_path, monkeypatch) -> None:
    replay = tmp_path / "game"
    final = await run_game(_settings(replay))

    def fail_registry_load() -> None:
        raise AssertionError("resume must use frozen meta, not YAML registry")

    monkeypatch.setattr(avalon_run, "load_registry", fail_registry_load)
    restored = await resume_game(ResumeRequest(replay=replay))
    assert restored.model_dump_json() == final.model_dump_json()


@pytest.mark.asyncio
async def test_terminal_opencode_replay_does_not_require_runtime_installation(tmp_path, monkeypatch) -> None:
    replay = tmp_path / "game"
    final = await run_game(_settings(replay))
    meta_path = replay / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["player"] = "opencode"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def fail_prepare(_meta) -> None:
        raise AssertionError("终局 replay 不应准备 OpenCode runtime")

    monkeypatch.setattr(avalon_run, "_prepare_runtime", fail_prepare)
    restored = await resume_game(ResumeRequest(replay=replay))

    assert restored.model_dump_json() == final.model_dump_json()


@pytest.mark.asyncio
async def test_real_actor_crash_then_production_resume_matches_clean_run(tmp_path, monkeypatch) -> None:
    """在 production actor 调用点真实注入崩溃，而不是事后裁剪一份完整日志。"""
    crashed_replay = tmp_path / "crashed"
    clean_replay = tmp_path / "clean"
    real_actor = avalon_run.RandomActor

    class InjectedCrash(FatalActorError):
        pass

    calls = 0

    class CrashActor:
        def __init__(self, generator) -> None:
            self._inner = real_actor(generator)

        async def speak(self, seat: int, view: Any, turn_id: str):
            nonlocal calls
            calls += 1
            if calls == 12:
                raise InjectedCrash("simulated process death")
            return await self._inner.speak(seat, view, turn_id)

        def commit_turn(self, seat: int) -> None:
            self._inner.commit_turn(seat)

        async def rollback(self, seat: int) -> None:
            await self._inner.rollback(seat)

    monkeypatch.setattr(avalon_run, "RandomActor", CrashActor)
    with pytest.raises(InjectedCrash):
        await run_game(_settings(crashed_replay, concurrency=1))
    assert (crashed_replay / "sentences.jsonl").stat().st_size > 0

    monkeypatch.setattr(avalon_run, "RandomActor", real_actor)
    resumed = await resume_game(ResumeRequest(replay=crashed_replay))
    clean = await run_game(_settings(clean_replay, concurrency=1))

    assert resumed.model_dump_json() == clean.model_dump_json()
    assert (crashed_replay / "sentences.jsonl").read_bytes() == (
        clean_replay / "sentences.jsonl"
    ).read_bytes()


@pytest.mark.asyncio
async def test_ten_player_full_role_game_reaches_evil_chat_and_stabs_only_good(tmp_path) -> None:
    replay = tmp_path / "full-role"
    await run_game(
        _settings(
            replay,
            players=10,
            seed=16,
            percival=True,
            morgana=True,
            mordred=True,
            oberon=True,
        )
    )
    meta = load_replay(ResumeRequest(replay=replay))
    assert set(meta.setup.roles) == {
        Role.MERLIN,
        Role.PERCIVAL,
        Role.SERVANT,
        Role.ASSASSIN,
        Role.MORGANA,
        Role.MORDRED,
        Role.OBERON,
    }

    rows = [
        json.loads(line)
        for line in (replay / "sentences.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    opening_row = next(
        row
        for row in rows
        if json.loads(row["action"] or "{}").get("kind") == "evil_chat_opening"
    )
    opening = json.loads(opening_row["action"])
    assert set(opening["roles"].values()) == {
        Role.ASSASSIN.value,
        Role.MORGANA.value,
        Role.MORDRED.value,
        Role.OBERON.value,
    }
    evil_seats = {int(no) for no in opening["roles"]}
    assert set(opening_row["deliveries"][0]["channels"]) == {
        *evil_seats,
        "audience",
    }
    evil_chat_rows = [
        row
        for row in rows
        if json.loads(row["action"] or "{}").get("kind") == "evil_chat"
    ]
    assert len(evil_chat_rows) == 4
    assert all(
        "broadcast" not in delivery["channels"]
        for row in evil_chat_rows
        for delivery in row["deliveries"]
    )
    assert all(
        set(row["deliveries"][0]["channels"]) == {*evil_seats, "audience"}
        for row in evil_chat_rows
    )

    assassination = next(
        json.loads(row["action"])
        for row in rows
        if json.loads(row["action"] or "{}").get("kind") == "assassinate"
    )
    target = assassination["target_no"]
    assert meta.setup.roles[target - 1].side is Side.GOOD


@pytest.mark.asyncio
async def test_production_random_game_with_lady_finishes(tmp_path) -> None:
    final = await run_game(
        _settings(tmp_path / "lady", players=7, seed=42, lady=True)
    )
    assert final.phase == "done"
