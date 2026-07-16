"""Journal 的落盘契约：句子日志、快照、续跑水位。

句子日志即 checkpoint：一行一句，写盘即推进；快照只是 resume 的加速器，不是正确性
的承重墙。这些测试保证：写出去的能读回来、快照能取最新、续跑 seq 从已有水位接续。
"""

from __future__ import annotations

import json

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


def test_append_and_load_roundtrip(tmp_path):
    j = DirJournal(tmp_path / "rep", resume=False)
    j.append_sentence(_sent(0))
    j.append_sentence(_sent(1, speaker="host", text="串场"))
    j.close()

    j2 = DirJournal(tmp_path / "rep", resume=True)
    rows = j2.load_sentences()
    j2.close()
    assert [r["seq"] for r in rows] == [0, 1]
    assert rows[1]["speaker"] == "host"
    assert rows[0]["deliveries"][0]["text"] == "你好"


def test_seq_continues_from_existing_watermark(tmp_path):
    j = DirJournal(tmp_path / "rep", resume=False)
    j.append_sentence(_sent(0))
    j.append_sentence(_sent(1))
    j.close()

    j3 = DirJournal(tmp_path / "rep", resume=True)
    assert j3.next_seq() == 2  # 续跑不重号、不回退
    j3.append_sentence(_sent(2))
    j3.close()
    j4 = DirJournal(tmp_path / "rep", resume=True)
    assert len(j4.load_sentences()) == 3
    j4.close()


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
    j.append_sentence(_sent(0))
    j.save_snapshot(0, b"")
    assert j.latest_snapshot() is None
    assert j.load_sentences() == []
    assert j.next_seq() == 0
    j.close()
