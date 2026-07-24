"""覆盖确认：4 位随机冷静码。

破坏性覆盖（重录视频、重烧封面）前，要求用户逐字输入当场随机生成的 4 位码
（小写字母+数字）。每次码不同、无法形成肌肉记忆——输入动作本身就是冷静期。
stdin 已关闭（管道/重定向到 EOF）时按取消兜底，不甩 traceback。
"""

from __future__ import annotations

import secrets
import string

from rich.console import Console
from rich.panel import Panel

_ALPHABET = string.ascii_lowercase + string.digits


def confirm_overwrite(console: Console, *, title: str, lines: list[str]) -> bool:
    """展示将覆盖的对象并索要随机码；用户输入完全一致才返回 True。"""
    code = "".join(secrets.choice(_ALPHABET) for _ in range(4))
    console.print(
        Panel.fit(
            "\n".join(lines)
            + f"\n\n输入 [bold cyan]{code}[/bold cyan] 确认覆盖，直接回车取消",
            title=title,
            border_style="yellow",
        )
    )
    try:
        answer = console.input("> ")
    except EOFError:
        return False
    return answer.strip() == code
