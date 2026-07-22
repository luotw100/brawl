"""顶层 CLI 的跨模块用户错误收口。"""

from __future__ import annotations

import pytest

from brawl_games import cli
from core.sr.files import UnsafePathError
from core.sr.journal import JournalCorruptionError, JournalInUseError


@pytest.mark.parametrize(
    "error",
    [
        JournalInUseError("replay busy"),
        JournalCorruptionError("broken journal"),
        UnsafePathError("unsafe lock"),
        FileExistsError("replay already exists"),
    ],
)
def test_top_cli_prints_journal_boundary_errors_without_traceback(
    error: Exception,
    monkeypatch,
    capsys,
) -> None:
    def fail(_argv: list[str]) -> None:
        raise error

    monkeypatch.setitem(cli.COMMANDS, "probe", fail)
    monkeypatch.setattr(cli.sys, "argv", ["brawl", "probe"])

    assert cli.main() == 1
    assert str(error) in capsys.readouterr().err
