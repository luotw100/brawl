"""录制落盘：DirRecorder 真写盘、NullRecorder 全空操作、open_recorder 工厂（不变量 #9）。

open_recorder 是空对象模式的落点：给了 --replay 返回会写盘的 DirRecorder，没给返回方法
全空的 NullRecorder——调用方零分叉、不再判断"要不要落盘"。
"""

from __future__ import annotations

import json

import pytest

from undercover.recorder import DirRecorder, NullRecorder, open_recorder


def test_dir_recorder_writes_three_artifacts(tmp_path) -> None:
    run_dir = tmp_path / "game1"
    rec = DirRecorder(run_dir)
    rec.write_meta({"started_at": "now", "players": 6})
    rec.record_event({"type": "game_start", "players": 6})
    rec.record_event({"type": "game_end", "winner": "civilians"})
    rec.record_llm_call({"phase": "describe", "ok": True})

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["players"] == 6

    events = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(e)["type"] for e in events] == ["game_start", "game_end"]

    calls = (run_dir / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(calls[0])["phase"] == "describe"


def test_dir_recorder_leaves_empty_llm_file_when_no_calls(tmp_path) -> None:
    """random 后端无 LLM 调用，也要留个空 llm_calls.jsonl（产物形状一致）。"""
    run_dir = tmp_path / "game2"
    DirRecorder(run_dir)
    assert (run_dir / "llm_calls.jsonl").exists()
    assert (run_dir / "llm_calls.jsonl").read_text(encoding="utf-8") == ""


def test_dir_recorder_refuses_existing_dir(tmp_path) -> None:
    """目录已存在即拒绝（exist_ok=False 的纵深防御）。"""
    with pytest.raises(FileExistsError):
        DirRecorder(tmp_path)  # tmp_path 一定已存在


def test_null_recorder_is_noop(tmp_path) -> None:
    """三个方法全空操作、不写任何文件。"""
    rec = NullRecorder()
    rec.write_meta({"a": 1})
    rec.record_event({"type": "x"})
    rec.record_llm_call({"ok": True})
    assert list(tmp_path.iterdir()) == []


def test_open_recorder_factory(tmp_path) -> None:
    assert isinstance(open_recorder(None), NullRecorder)
    run_dir = tmp_path / "game3"
    rec = open_recorder(run_dir)
    assert isinstance(rec, DirRecorder)
    assert run_dir.exists()
