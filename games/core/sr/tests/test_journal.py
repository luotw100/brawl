"""Journal 的严格 replay 边界、turn 事务与崩溃一致性回归。"""

from __future__ import annotations

import json
import os

import pytest

from core.sr.files import UnsafePathError
from core.sr.journal import (
    DirJournal,
    JournalCorruptionError,
    JournalInUseError,
    NullJournal,
    load_replay_sentences,
)
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


def test_replay_writer_lock_prevents_lost_update_and_releases_on_close(tmp_path) -> None:
    rep = tmp_path / "rep"
    first = DirJournal(rep, resume=False)
    first.append_batch([_sent(1, turn="turn.0")])

    with pytest.raises(JournalInUseError, match="已有生成或续跑任务"):
        DirJournal(rep, resume=True)

    first.close()
    first.close()  # close 必须幂等，方便 finally 与异常清理重叠。
    second = DirJournal(rep, resume=True)
    second.append_batch([_sent(2, turn="turn.1")])
    rows = second.load_sentences()
    second.close()

    assert [row["turn_id"] for row in rows] == ["turn.0", "turn.1"]
    assert [row["commit_id"] for row in rows] == [0, 1]


def test_failed_resume_constructor_releases_writer_lock(tmp_path) -> None:
    rep = tmp_path / "rep"
    rep.mkdir()
    (rep / "sentences.jsonl").write_bytes(b"torn")

    with pytest.raises(JournalCorruptionError):
        DirJournal(rep, resume=True)

    (rep / "sentences.jsonl").write_bytes(b"")
    reopened = DirJournal(rep, resume=True)
    reopened.close()


def test_replace_failure_leaves_previous_turn_as_only_truth(tmp_path, monkeypatch) -> None:
    """死在 tmp fsync 之后、replace 之前：主日志仍是上一个完整 turn。"""
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent(1), _sent(2)])
    committed = (rep / "sentences.jsonl").read_bytes()

    failed = _sent(1, turn="turn.1")
    with monkeypatch.context() as patch:
        patch.setattr(
            os,
            "replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash")),
        )
        with pytest.raises(OSError, match="crash"):
            journal.append_batch([failed, _sent(2, turn="turn.1")])
    journal.close()

    assert (rep / "sentences.jsonl").read_bytes() == committed
    assert failed.seq == -1
    assert list(rep.glob(".sentences.jsonl.*.tmp")) == []

    resumed = DirJournal(rep, resume=True)
    replacement = [_sent(1, turn="turn.1"), _sent(2, turn="turn.1")]
    resumed.append_batch(replacement)
    resumed.close()
    assert [row["commit_id"] for row in DirJournal(rep, resume=True).load_sentences()] == [0, 0, 1, 1]


def test_unrelated_stale_temp_file_is_ignored_and_preserved(tmp_path) -> None:
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent()])
    journal.close()
    stale = rep / ".sentences.jsonl.abandoned.tmp"
    stale.write_bytes(b'{"torn":')

    resumed = DirJournal(rep, resume=True)
    assert len(resumed.load_sentences()) == 1
    resumed.append_batch([_sent(turn="turn.1")])
    resumed.close()

    assert stale.read_bytes() == b'{"torn":'
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


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_line_separators_are_content_not_jsonl_boundaries(tmp_path, separator: str) -> None:
    rep = tmp_path / "rep"
    text = f"前{separator}后"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent(text=text)])
    journal.close()

    [sentence] = load_replay_sentences(rep)
    assert sentence.raw == text
    assert sentence.deliveries[0].text == text
    assert DirJournal(rep, resume=True).load_sentences()[0]["raw"] == text


def test_replay_reader_rejects_unknown_or_coerced_sentence_fields(tmp_path) -> None:
    rep = tmp_path / "rep"
    journal = DirJournal(rep, resume=False)
    journal.append_batch([_sent()])
    journal.close()
    row = json.loads((rep / "sentences.jsonl").read_text(encoding="utf-8"))
    row["seq"] = "0"
    row["legacy"] = True
    (rep / "sentences.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(JournalCorruptionError, match="合法 Sentence"):
        load_replay_sentences(rep)


def test_resume_rejects_symlinked_sentence_log(tmp_path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"")
    rep = tmp_path / "rep"
    rep.mkdir()
    (rep / "sentences.jsonl").symlink_to(outside)

    with pytest.raises(UnsafePathError, match="符号链接"):
        DirJournal(rep, resume=True)


def test_meta_write_rejects_symlink_without_touching_target(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    journal = DirJournal(tmp_path / "rep", resume=False)
    (journal.run_dir / "meta.json").symlink_to(outside)

    with pytest.raises(UnsafePathError, match="符号链接"):
        journal.write_meta({"player": "dummy"})
    assert outside.read_text(encoding="utf-8") == "keep"


def test_meta_is_create_once_even_when_reopened_for_resume(tmp_path) -> None:
    rep = tmp_path / "rep"
    original = {"game": "dummy", "players": 3}
    journal = DirJournal(rep, resume=False)
    journal.write_meta(original)
    journal.close()
    before = (rep / "meta.json").read_bytes()

    resumed = DirJournal(rep, resume=True)
    with pytest.raises(FileExistsError, match="meta.json"):
        resumed.write_meta(original)
    with pytest.raises(FileExistsError, match="meta.json"):
        resumed.write_meta({"game": "tampered", "players": 99})
    resumed.close()

    assert (rep / "meta.json").read_bytes() == before


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
    assert journal.load_sentences() == []
    assert journal.load_batches() == []
    journal.close()
