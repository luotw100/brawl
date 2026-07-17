"""Journal 的落盘契约：句子日志、快照、续跑水位。

句子日志即 checkpoint：一个 turn 合批一次落盘；快照只是 resume 的加速器，不是正确性
的承重墙。这些测试保证：写出去的能读回来、快照能取最新、续跑 seq 从已有水位接续、
torn 尾行（崩溃留半行）只丢最后一行不殃及前文。
"""

from __future__ import annotations

import json

import pytest

from core.sr.journal import DirJournal, NullJournal
from core.sr.types import Delivery, Sentence


def _sent(seq: int, speaker: int | str = 1, text: str = "你好") -> Sentence:
    return Sentence(
        seq=seq,
        turn_id=f"t{seq}",
        speaker=speaker,  # type: ignore[arg-type]
        raw=text,
        deliveries=[Delivery(text=text, channels=["broadcast"])],
        action=json.dumps({"kind": "say", "seq": seq}),
    )


def test_append_batch_and_load_roundtrip(tmp_path):
    j = DirJournal(tmp_path / "rep", resume=False)
    last = j.append_batch([_sent(-1), _sent(-1, speaker="host", text="串场")])
    assert last == 1
    j.close()

    j2 = DirJournal(tmp_path / "rep", resume=True)
    rows = j2.load_sentences()
    j2.close()
    assert [r["seq"] for r in rows] == [0, 1]  # seq 由 journal 落批时赋
    assert rows[1]["speaker"] == "host"
    assert rows[0]["deliveries"][0]["text"] == "你好"


def test_seq_continues_from_existing_watermark(tmp_path):
    j = DirJournal(tmp_path / "rep", resume=False)
    j.append_batch([_sent(-1), _sent(-1)])
    j.close()

    j3 = DirJournal(tmp_path / "rep", resume=True)
    assert j3.next_seq() == 2  # 续跑不重号、不回退
    j3.append_batch([_sent(-1)])
    j3.close()
    j4 = DirJournal(tmp_path / "rep", resume=True)
    assert len(j4.load_sentences()) == 3
    j4.close()


def test_torn_last_line_is_dropped(tmp_path):
    """崩溃留半行：只丢 torn 尾行，前文完好。"""
    j = DirJournal(tmp_path / "rep", resume=False)
    j.append_batch([_sent(-1), _sent(-1)])
    j.close()
    with (tmp_path / "rep" / "sentences.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "turn_id": "t2", "spea')  # 写到一半断电
    j2 = DirJournal(tmp_path / "rep", resume=True)
    rows = j2.load_sentences()
    j2.close()
    assert [r["seq"] for r in rows] == [0, 1]


def test_corrupt_middle_line_raises(tmp_path):
    """中段坏行是真损坏：fail-fast 抛错，不吞数据。"""
    j = DirJournal(tmp_path / "rep", resume=False)
    j.append_batch([_sent(-1), _sent(-1)])
    j.close()
    path = tmp_path / "rep" / "sentences.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(lines[0] + "\n" + "{bad json\n" + lines[1] + "\n", encoding="utf-8")
    j2 = DirJournal(tmp_path / "rep", resume=True)
    with pytest.raises(json.JSONDecodeError):
        j2.load_sentences()
    j2.close()


def test_snapshot_save_and_latest(tmp_path):
    j = DirJournal(tmp_path / "rep", resume=False)
    assert j.latest_snapshot() is None
    j.save_snapshot(3, b'{"step": 3}')
    j.save_snapshot(7, b'{"step": 7}')
    j.close()

    j2 = DirJournal(tmp_path / "rep", resume=True)
    seq, blob = j2.latest_snapshot()  # type: ignore[misc]
    assert seq == 7 and blob == b'{"step": 7}'
    j2.close()


def test_torn_snapshot_falls_back_to_full_replay(tmp_path):
    """快照写到一半：按无快照处理（正确性不靠它，全量重放兜底）。"""
    j = DirJournal(tmp_path / "rep", resume=False)
    j.save_snapshot(3, b'{"step": 3}')
    j.close()
    (tmp_path / "rep" / "_snapshot.json").write_text('{"seq": 3, "state": "aGVsb', encoding="utf-8")
    j2 = DirJournal(tmp_path / "rep", resume=True)
    assert j2.latest_snapshot() is None
    j2.close()


def test_meta_and_avatar_written(tmp_path):
    j = DirJournal(tmp_path / "rep", resume=False)
    j.write_meta({"player": "dummy"})
    src = tmp_path / "a.png"
    src.write_bytes(b"png")
    j.write_avatar(1, src)
    j.close()
    meta = json.loads((tmp_path / "rep" / "meta.json").read_text(encoding="utf-8"))
    assert meta["player"] == "dummy"
    assert (tmp_path / "rep" / "avatars" / "1.png").read_bytes() == b"png"


def test_null_journal_is_noop():
    j = NullJournal()
    j.write_meta({})
    assert j.append_batch([_sent(0)]) == -1
    j.save_snapshot(0, b"")
    assert j.latest_snapshot() is None
    assert j.load_sentences() == []
    assert j.next_seq() == 0
    j.close()
