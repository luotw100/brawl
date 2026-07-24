"""覆盖确认的随机冷静码：逐字匹配才放行，EOF 按取消兜底。"""

from __future__ import annotations

from rich.console import Console

import brawl.runtime.confirm as confirm_mod
from brawl.runtime.confirm import confirm_overwrite


def _console() -> Console:
    return Console(file=open("/dev/null", "w"))  # 测试不刷屏


def _fixed_code(monkeypatch, code: str = "k7q2") -> None:
    """把随机码钉死：逐位返回，长度与字符集由实现决定（这里只钉结果）。"""
    chars = iter(code)
    monkeypatch.setattr(confirm_mod.secrets, "choice", lambda _alphabet: next(chars))


def test_exact_code_accepts(monkeypatch) -> None:
    _fixed_code(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "k7q2")
    assert confirm_overwrite(_console(), title="t", lines=["x"]) is True


def test_wrong_or_empty_code_cancels(monkeypatch) -> None:
    for answer in ("", "XXXX", "k7q3", " "):
        _fixed_code(monkeypatch)
        monkeypatch.setattr("builtins.input", lambda _prompt="", _a=answer: _a)
        assert confirm_overwrite(_console(), title="t", lines=["x"]) is False


def test_eof_cancels_without_traceback(monkeypatch) -> None:
    _fixed_code(monkeypatch)

    def eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert confirm_overwrite(_console(), title="t", lines=["x"]) is False
