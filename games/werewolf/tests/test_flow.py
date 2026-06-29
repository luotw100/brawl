from __future__ import annotations

import json

from werewolf.run import run_game
from werewolf.settings import WerewolfSettings


def _events(run_dir) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]


async def test_random_game_writes_replay(tmp_path) -> None:
    run_dir = tmp_path / "werewolf"
    final = await run_game(WerewolfSettings(player="random", seed=1, replay=run_dir))

    assert final.finished
    assert final.winner in {"good", "werewolf"}
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "llm_calls.jsonl").exists()
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["game"] == "werewolf"
    assert meta["setup"]["board"] == "pre-witch-hunter-idiot-12"

    events = _events(run_dir)
    assert events[0]["type"] == "game_start"
    assert any(ev["type"] == "wolf_kill" for ev in events)
    assert any(ev["type"] == "game_end" for ev in events)
    # 赛后复盘在 game_end 之后：全员各一段 banter
    assert sum(1 for ev in events if ev["type"] == "banter") == 12


async def test_resume_artifacts_exist_for_replay(tmp_path) -> None:
    run_dir = tmp_path / "werewolf"
    await run_game(WerewolfSettings(player="random", seed=2, replay=run_dir))
    assert (run_dir / "state.sqlite").exists()
    assert (run_dir / "_resume.jsonl").exists()


async def test_parallel_phases_carry_per_seat_call_ids(tmp_path) -> None:
    """狼刀与放逐投票的事件 payload 必须给每席位带 call_id，且能对齐到 llm_calls（契约回归）。"""
    run_dir = tmp_path / "werewolf"
    await run_game(WerewolfSettings(player="random", seed=3, replay=run_dir))
    events = _events(run_dir)
    llm_ids = {json.loads(line)["call_id"] for line in (run_dir / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()}

    for ev in events:
        if ev["type"] in {"wolf_kill", "vote_result"}:
            assert ev["votes"], ev
            for v in ev["votes"]:
                assert "call_id" in v
                assert v["call_id"] in llm_ids
