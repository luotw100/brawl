"""Journal 的 turn 事务、快照与崩溃一致性回归。"""

from __future__ import annotations

import json
import os

import pytest

from core.sr.journal import DirJournal, JournalCorruptionError, NullJournal
from core.sr.types import Delivery, Sentence


def _sent(speaker: int | str = 1, text: str = "你好", turn: str = "turn.0") -> Sentence:
    return Sentence(
        seq=-1,
        turn_id=turn,
        speaker=speaker,  # type: ignore[arg-type]
        raw=text,
        prompt=f"请 {speaker} 号行动",
        deliveries=[Delivery(text=text, channels=["broadcast"])],
        action=json.dumps({"kind": "say", "text": text}),
    )


def test_append_batch_assigns_seq_and_commit_id(tmp_path) -> None:
    journal = DirJournal(tmp_path / "rep", resume=False)
    first = [_sent(1), _sent(2), _sent("host", "串场")]
    assert journal.append_batch(first) == 2
    second = [_sent(1, turn="turn.1")]
    assert journal.append_batch(second) == 3

    rows = journal.load_sentences()
    batches = journal.load_batches()
    journal.close()

    assert [row["seq"] for row in rows] == [0, 1, 2, 3]
    assert [row["commit_id"] for row in rows] == [0, 0, 0, 1]
    assert [[row["seq"] for row in batch] for batch in batches] == [[0, 1, 2], [3]]
    assert [sentence.seq for sentence in first] == [0, 1, 2]
    assert [sentence.commit_id for sentence in first] == [0, 0, 0]


def test_seq_and_commit_continue_after_reopen(tmp_path) -> None:
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent(1), _sent(2)])
    journal.close()

    resumed = DirJournal(rep, resume=True)
    sentence = _sent(1, turn="turn.1")
    assert resumed.append_batch([sentence]) == 2
    assert sentence.seq == 2
    assert sentence.commit_id == 1
    resumed.close()

    reopened = DirJournal(rep, resume=True)
    assert reopened.last_seq() == 2
    assert [row["commit_id"] for row in reopened.load_sentences()] == [0, 0, 1]
    reopened.close()


def test_replace_failure_leaves_previous_turn_as_only_truth(tmp_path, monkeypatch) -> None:
    """死在 tmp fsync 之后、replace 之前：主日志仍是上一个完整 turn。"""
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent(1), _sent(2)])
    committed = (rep / "sentences.jsonl").read_bytes()

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", lambda _src, _dst: (_ for _ in ()).throw(OSError("crash")))
        with pytest.raises(OSError, match="crash"):
            journal.append_batch([_sent(1, turn="turn.1"), _sent(2, turn="turn.1")])
    journal.close()

    assert (rep / "sentences.jsonl").read_bytes() == committed
    assert (rep / ".sentences.jsonl.tmp").exists()

    resumed = DirJournal(rep, resume=True)
    replacement = [_sent(1, turn="turn.1"), _sent(2, turn="turn.1")]
    resumed.append_batch(replacement)
    resumed.close()
    assert [row["commit_id"] for row in DirJournal(rep, resume=True).load_sentences()] == [0, 0, 1, 1]


def test_stale_temp_file_is_ignored_and_overwritten(tmp_path) -> None:
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent()])
    journal.close()
    (rep / ".sentences.jsonl.tmp").write_bytes(b'{"torn":')

    resumed = DirJournal(rep, resume=True)
    assert len(resumed.load_sentences()) == 1
    resumed.append_batch([_sent(turn="turn.1")])
    resumed.close()

    assert not (rep / ".sentences.jsonl.tmp").exists()
    assert len(DirJournal(rep, resume=True).load_sentences()) == 2


@pytest.mark.parametrize(
    "damaged",
    [
        b'{"seq": 0',
        b'{"seq":0,"commit_id":0}\n{bad json\n',
        b'{"seq":2,"commit_id":0}\n',
        b'{"seq":0,"commit_id":3}\n',
    ],
)
def test_damaged_main_log_fails_fast(tmp_path, damaged: bytes) -> None:
    rep = tmp_path / "rep"
    rep.mkdir()
    (rep / "sentences.jsonl").write_bytes(damaged)
    with pytest.raises(JournalCorruptionError):
        DirJournal(rep, resume=True)


def test_snapshot_is_atomic_bound_cache(tmp_path) -> None:
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent()])
    journal.save_snapshot(0, b'{"step":1}')
    journal.append_batch([_sent(turn="turn.1")])
    journal.save_snapshot(1, b'{"step":2}')
    journal.close()

    resumed = DirJournal(rep, resume=True)
    assert resumed.latest_snapshot() == (1, b'{"step":2}')
    resumed.close()


def test_older_snapshot_remains_valid_after_later_commits(tmp_path) -> None:
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent()])
    journal.save_snapshot(0, b'{"step":1}')
    journal.append_batch([_sent(turn="turn.1")])
    journal.close()

    resumed = DirJournal(rep, resume=True)
    assert resumed.latest_snapshot() == (0, b'{"step":1}')
    resumed.close()


def test_snapshot_must_point_to_committed_tail(tmp_path) -> None:
    journal = DirJournal(tmp_path / "rep", resume=False)
    journal.append_batch([_sent(), _sent(2)])
    with pytest.raises(ValueError, match="committed tail 1"):
        journal.save_snapshot(0, b"state")
    journal.close()


def test_corrupt_snapshot_falls_back_to_full_replay(tmp_path) -> None:
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent()])
    journal.save_snapshot(0, b'{"step":1}')
    journal.close()
    (rep / "_snapshot.json").write_text('{"seq":0,"state":"e30="}', encoding="utf-8")

    resumed = DirJournal(rep, resume=True)
    assert resumed.latest_snapshot() is None
    resumed.close()


def test_meta_and_avatar_written(tmp_path) -> None:
    journal = DirJournal(tmp_path / "rep", resume=False)
    journal.write_meta({"player": "dummy"})
    source = tmp_path / "a.png"
    source.write_bytes(b"png")
    journal.write_avatar(1, source)
    journal.close()

    meta = json.loads((tmp_path / "rep" / "meta.json").read_text(encoding="utf-8"))
    assert meta["player"] == "dummy"
    assert (tmp_path / "rep" / "avatars" / "1.png").read_bytes() == b"png"


def test_null_journal_assigns_runtime_ids_without_disk() -> None:
    journal = NullJournal()
    first = [_sent(1), _sent(2)]
    second = [_sent(3, turn="turn.1")]
    assert journal.append_batch(first) == 1
    assert journal.append_batch(second) == 2
    assert [sentence.seq for sentence in [*first, *second]] == [0, 1, 2]
    assert [sentence.commit_id for sentence in [*first, *second]] == [0, 0, 1]
    assert journal.last_seq() == 2
    journal.save_snapshot(2, b"")
    assert journal.latest_snapshot() is None
    assert journal.load_sentences() == []
    assert journal.load_batches() == []
    journal.close()
