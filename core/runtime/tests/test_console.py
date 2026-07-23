"""Console replay reader 的严格输入边界。"""

from __future__ import annotations

import pytest

from core.runtime.console import ConsoleError, main


def test_console_wraps_missing_replay_as_user_error(tmp_path) -> None:
    with pytest.raises(ConsoleError, match="Sentence Runtime 日志"):
        main([str(tmp_path / "missing")])


def test_console_rejects_symlinked_sentence_log(tmp_path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"")
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "sentences.jsonl").symlink_to(outside)

    with pytest.raises(ConsoleError, match="Sentence Runtime 日志"):
        main([str(replay)])


@pytest.mark.parametrize(
    "argv",
    [
        ["replay", "--no-audience"],
        ["replay", "--pace", "0"],
        ["replay", "--pace", "nan"],
        ["replay", "--pace", "inf"],
        ["replay", "--pace", "-inf"],
    ],
)
def test_console_rejects_removed_or_invalid_render_flags(argv) -> None:
    with pytest.raises(SystemExit):
        main(argv)
