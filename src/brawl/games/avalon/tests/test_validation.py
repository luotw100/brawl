"""版本化 Avalon replay meta 的边界测试。"""

from __future__ import annotations

import json
import random
from contextlib import asynccontextmanager

import pytest

from brawl.games.avalon import run as avalon_run
from brawl.games.avalon.registry import load_registry
from brawl.games.avalon.rules import DEFAULT_BOARD
from brawl.games.avalon.validation import (
    AvalonReplayMeta,
    load_replay,
    resolve_and_validate,
)
from brawl.games.run import RunSettings, SettingsError


def _meta(tmp_path) -> AvalonReplayMeta:
    registry = load_registry()
    settings = RunSettings(board=DEFAULT_BOARD, replay=tmp_path / "fresh", seed=7, backend="random")
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    return AvalonReplayMeta(
        started_at="2026-07-18T12:00:00+00:00",
        backend=settings.backend,
        concurrency=settings.concurrency,
        render=settings.render,
        seed=settings.seed,
        setup=setup,
        model_labels={},
    )


def _write_replay_at(replay, payload: dict):
    replay.mkdir()
    (replay / "sentences.jsonl").write_text("", encoding="utf-8")
    (replay / "meta.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return replay


def _write_replay(tmp_path, payload: dict):
    return _write_replay_at(tmp_path / "replay", payload)


def test_meta_freezes_all_prompt_model_and_aliyun_voice_inputs(tmp_path) -> None:
    meta = _meta(tmp_path)
    setup = meta.setup
    assert meta.seed == 7
    players = setup.players
    assert len(setup.tones) == players
    assert len(setup.strategy_systems) == players
    assert len(setup.opencode_models) == players
    assert len(setup.names) == players
    assert len(setup.voices) == players
    assert len(setup.voice_designs) == players
    assert setup.agent_protocol == 1
    dumped = setup.model_dump()
    assert "self_hosted_voices" not in dumped
    assert "host_self_hosted_voice" not in dumped


def test_old_meta_without_agent_protocol_defaults_to_v1(tmp_path) -> None:
    raw = _meta(tmp_path).model_dump(mode="json")
    del raw["setup"]["agent_protocol"]
    replay = _write_replay(tmp_path, raw)

    restored = load_replay(replay)

    assert restored.setup.agent_protocol == 1


def test_old_meta_without_agent_config_defaults_to_none(tmp_path) -> None:
    raw = _meta(tmp_path).model_dump(mode="json")
    del raw["agent_config"]
    replay = _write_replay(tmp_path, raw)

    restored = load_replay(replay)

    assert restored.agent_config is None


def test_new_opencode_game_selects_latest_role_agent_protocol(tmp_path) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )

    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))

    assert setup.agent_protocol == 24
    assert setup.opencode_models == ("deepseek/deepseek-v4-flash",) * 10


def test_protocol10_meta_freezes_current_agent_config(tmp_path, monkeypatch) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    live_config = avalon_run.trusted_config()
    frozen_servant = live_config["agent/avalon-servant.md"]
    monkeypatch.setattr(
        avalon_run,
        "trusted_config",
        lambda protocol: live_config,
    )

    meta = avalon_run._build_meta(registry, setup, settings)
    live_config["agent/avalon-servant.md"] = "changed-after-build"

    assert meta.agent_config is not None
    assert meta.agent_config["agent/avalon-servant.md"] == frozen_servant


@pytest.mark.asyncio
async def test_protocol10_open_actors_uses_meta_agent_config_without_live_read(
    tmp_path,
    monkeypatch,
) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    frozen = avalon_run.trusted_config()
    meta = AvalonReplayMeta(
        started_at="2026-07-18T12:00:00+00:00",
        backend="opencode",
        concurrency=3,
        render=False,
        seed=11,
        setup=setup,
        model_labels={},
        agent_config=frozen,
    )
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_runtime(installation, replay, seats, trusted_config=None):
        captured["trusted_config"] = trusted_config
        yield object()

    class FakeBackend:
        def __init__(self, *, connection, seat_models):
            captured["seat_models"] = seat_models

        async def assert_models_available(self, refs):
            captured["model_refs"] = tuple(refs)

        async def close(self):
            captured["closed"] = True

    def fail_live_read():
        raise AssertionError("protocol 10 恢复不得读取活 trusted_config")

    monkeypatch.setattr(avalon_run, "open_opencode_runtime", fake_runtime)
    monkeypatch.setattr(avalon_run, "OpenCodeBackend", FakeBackend)
    monkeypatch.setattr(avalon_run, "trusted_config", fail_live_read)

    async with avalon_run._open_actors(
        meta,
        tmp_path,
        object(),
        object(),
    ) as actors:
        assert len(actors) == 10

    assert captured["trusted_config"] == frozen
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_protocol3_open_actors_uses_legacy_live_tool_schema(
    tmp_path,
    monkeypatch,
) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )
    latest = resolve_and_validate(settings, registry, random.Random(settings.seed))
    setup = latest.model_copy(update={"agent_protocol": 3})
    meta = AvalonReplayMeta(
        started_at="2026-07-18T12:00:00+00:00",
        backend="opencode",
        concurrency=3,
        render=False,
        seed=11,
        setup=setup,
        model_labels={},
        agent_config=None,
    )
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_runtime(installation, replay, seats, trusted_config=None):
        captured["trusted_config"] = trusted_config
        yield object()

    class FakeBackend:
        def __init__(self, *, connection, seat_models):
            pass

        async def assert_models_available(self, refs):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(avalon_run, "open_opencode_runtime", fake_runtime)
    monkeypatch.setattr(avalon_run, "OpenCodeBackend", FakeBackend)

    async with avalon_run._open_actors(
        meta,
        tmp_path,
        object(),
        object(),
    ):
        pass

    bundle = captured["trusted_config"]
    assert isinstance(bundle, dict)
    assert "tool/submit_speech.js" not in bundle
    for name in ("submit_team", "submit_statement", "submit_assassination"):
        assert '"speech"' not in bundle[f"tool/{name}.js"]


def test_protocol10_meta_rejects_missing_position_tool(tmp_path) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    bundle = avalon_run.trusted_config()
    del bundle["tool/submit_statement.js"]

    with pytest.raises(ValueError, match="submit_statement"):
        AvalonReplayMeta(
            started_at="2026-07-18T12:00:00+00:00",
            backend="opencode",
            concurrency=3,
            render=False,
            seed=11,
            setup=setup,
            model_labels={},
            agent_config=bundle,
        )


def test_protocol15_meta_rejects_missing_speech_tool(tmp_path) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    bundle = avalon_run.trusted_config(14)

    with pytest.raises(ValueError, match="submit_speech"):
        AvalonReplayMeta(
            started_at="2026-07-18T12:00:00+00:00",
            backend="opencode",
            concurrency=3,
            render=False,
            seed=11,
            setup=setup,
            model_labels={},
            agent_config=bundle,
        )


def test_protocol14_meta_accepts_its_older_closed_config(tmp_path) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )
    latest = resolve_and_validate(settings, registry, random.Random(settings.seed))
    setup = latest.model_copy(update={"agent_protocol": 14})
    bundle = avalon_run.trusted_config()
    del bundle["tool/submit_speech.js"]

    meta = AvalonReplayMeta(
        started_at="2026-07-18T12:00:00+00:00",
        backend="opencode",
        concurrency=3,
        render=False,
        seed=11,
        setup=setup,
        model_labels={},
        agent_config=bundle,
    )

    assert meta.setup.agent_protocol == 14


def test_protocol10_meta_rejects_unexpected_agent_config_file(tmp_path) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    bundle = avalon_run.trusted_config()
    bundle["plugin/arbitrary.js"] = "export default {}"

    with pytest.raises(ValueError, match="unexpected=.*plugin/arbitrary"):
        AvalonReplayMeta(
            started_at="2026-07-18T12:00:00+00:00",
            backend="opencode",
            concurrency=3,
            render=False,
            seed=11,
            setup=setup,
            model_labels={},
            agent_config=bundle,
        )


def test_protocol10_meta_rejects_empty_agent_config_file(tmp_path) -> None:
    registry = load_registry()
    settings = RunSettings(
        board=DEFAULT_BOARD,
        replay=tmp_path / "fresh-opencode",
        seed=11,
        backend="opencode",
        models=("ds-v4-flash",) * 10,
    )
    setup = resolve_and_validate(settings, registry, random.Random(settings.seed))
    bundle = avalon_run.trusted_config()
    bundle["tool/submit_team.js"] = " \n"

    with pytest.raises(ValueError, match="文件内容不能为空.*submit_team"):
        AvalonReplayMeta(
            started_at="2026-07-18T12:00:00+00:00",
            backend="opencode",
            concurrency=3,
            render=False,
            seed=11,
            setup=setup,
            model_labels={},
            agent_config=bundle,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw.update(game="werewolf"), "Avalon schema v1"),
        (lambda raw: raw.update(schema_version=2), "Avalon schema v1"),
        (lambda raw: raw["setup"]["tones"].pop(), "逐座位列表长度"),
        (lambda raw: raw["setup"]["opencode_models"].__setitem__(0, " provider/model "), "providerID/modelID"),
        (lambda raw: raw["setup"].update(first_leader=99), "first_leader"),
        (
            lambda raw: (
                raw["setup"].update(agent_protocol=2),
                raw["setup"].update(lady_holder=None),
            ),
            "必须启用湖中仙女",
        ),
        (
            lambda raw: (
                raw.update(backend="opencode"),
                raw["setup"].update(agent_protocol=4),
                raw["setup"].update(
                    opencode_models=["deepseek/deepseek-v4-flash"] * 10
                ),
                raw.update(agent_config=None),
            ),
            "必须冻结 agent_config",
        ),
        (
            lambda raw: raw["setup"].update(agent_protocol=9),
            "random 对局只支持 agent_protocol=1",
        ),
        (lambda raw: raw["setup"].update(unknown="debt"), "extra_forbidden"),
    ],
)
def test_resume_rejects_wrong_game_version_or_malformed_setup(
    tmp_path,
    mutation,
    message: str,
) -> None:
    raw = _meta(tmp_path).model_dump(mode="json")
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
        load_replay(replay)


def test_resume_wraps_corrupt_sentence_log_as_settings_error(tmp_path) -> None:
    replay = _write_replay(tmp_path, _meta(tmp_path).model_dump(mode="json"))
    (replay / "sentences.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(SettingsError, match="Sentence Runtime 日志"):
        load_replay(replay)


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
        _meta(tmp_path).model_dump(mode="json"),
    )
    if entry == "replay":
        linked_replay = tmp_path / "linked-replay"
        linked_replay.symlink_to(request, target_is_directory=True)
        request = linked_replay
    else:
        path = request / entry
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
        request = replay
    else:
        request = _write_replay(tmp_path, _meta(tmp_path).model_dump(mode="json"))
        path = request / entry
        path.unlink()
        path.mkdir()

    with pytest.raises(SettingsError, match=message):
        load_replay(request)


def test_resume_wraps_missing_replay_as_settings_error(tmp_path) -> None:
    with pytest.raises(SettingsError, match="Sentence Runtime 日志"):
        load_replay(tmp_path / "missing")
