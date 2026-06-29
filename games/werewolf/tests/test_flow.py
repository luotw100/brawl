from __future__ import annotations

import json

from werewolf.run import run_game
from werewolf.settings import WerewolfSettings


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

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["type"] == "game_start"
    assert any(ev["type"] == "wolf_kill" for ev in events)
    assert events[-2]["type"] == "game_end" or events[-1]["type"] == "game_end"


async def test_resume_artifacts_exist_for_replay(tmp_path) -> None:
    run_dir = tmp_path / "werewolf"
    await run_game(WerewolfSettings(player="random", seed=2, replay=run_dir))
    assert (run_dir / "state.sqlite").exists()
    assert (run_dir / "_resume.jsonl").exists()

