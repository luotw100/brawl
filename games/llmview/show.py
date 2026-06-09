"""chat-view：把 llm_calls.jsonl 第 N 行那次模型调用，像普通 chatbot 一样渲染出来。

跨玩法工具(与 voice-forge 同性质，不属于任何玩法)：输入 = 一个 llm_calls.jsonl + 行号，
输出 = 这次调用的完整聊天过程(system/user 各色气泡 + 模型回复)。只认各玩法统一的通用字段
(messages/output/usage/latency_ms/ok/error，见 recorder.py 落盘契约)，不碰任何玩法语义。

行号是 1-based(与编辑器/`sed -n` 一致)。文件缺失/行号越界/坏 JSON 都给干净提示、不甩 traceback。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# role(messages 里的) → (显示名, 边框色)。落盘的 role 取值见 player.py 的 _ROLE。
_ROLE_STYLE: dict[str, tuple[str, str]] = {
    "system": ("SYSTEM", "yellow"),
    "human": ("USER", "cyan"),
    "user": ("USER", "cyan"),
    "ai": ("ASSISTANT", "green"),
    "assistant": ("ASSISTANT", "green"),
}


class ViewError(Exception):
    """用户可见的输入错误(文件不存在/行号越界/坏行)——cli 只打印消息、不甩 traceback。"""


def _read_line(path: Path, line_no: int) -> dict[str, Any]:
    """取第 line_no 行(1-based)并解析为 dict；越界或坏 JSON 抛 ViewError。"""
    if not path.is_file():
        raise ViewError(f"文件不存在：{path}")
    if line_no < 1:
        raise ViewError(f"行号须 ≥ 1(1-based)，收到 {line_no}")
    with path.open(encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            if i == line_no:
                raw = raw.strip()
                if not raw:
                    raise ViewError(f"第 {line_no} 行是空行")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    raise ViewError(f"第 {line_no} 行不是合法 JSON：{e}") from e
    raise ViewError(f"行号越界：文件只有 {i} 行，要第 {line_no} 行")


def _bubble(console: Console, role: str, content: str) -> None:
    name, color = _ROLE_STYLE.get(role, (role.upper() or "?", "white"))
    console.print(Panel(Text(content), title=name, title_align="left", border_style=color))


def _meta_line(call: dict[str, Any]) -> Text:
    """顶部元信息条：有就显示、缺就跳过(空对象/前置判断，渲染不为可选字段分叉)。"""
    bits: list[str] = []
    for key in ("model", "phase", "call_id", "seat_no"):
        if call.get(key) is not None:
            bits.append(f"{key}={call[key]}")
    if call.get("latency_ms") is not None:
        bits.append(f"{call['latency_ms']}ms")
    usage = call.get("usage") or {}
    if usage:
        bits.append("usage=" + ",".join(f"{k}:{v}" for k, v in usage.items()))
    return Text("  ".join(bits), style="dim")


def render(call: dict[str, Any], console: Console) -> None:
    console.print(_meta_line(call))
    for msg in call.get("messages", []):
        _bubble(console, str(msg.get("role", "?")), str(msg.get("content", "")))
    # 模型本次的回复：ok 时是 output 气泡；失败时红色 error。
    if call.get("ok", True):
        _bubble(console, "assistant", str(call.get("output") or ""))
    else:
        console.print(Panel(Text(str(call.get("error") or "(无错误信息)")), title="ERROR", title_align="left", border_style="red"))


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help") or len(argv) < 2:
        print("用法: brawl chat-view <llm_calls.jsonl> <行号>\n  行号为 1-based(与编辑器一致)。")
        if argv and argv[0] in ("-h", "--help"):
            return
        raise ViewError("需要两个参数：jsonl 路径 与 行号")
    path = Path(argv[0]).expanduser()
    try:
        line_no = int(argv[1])
    except ValueError as e:
        raise ViewError(f"行号必须是整数，收到 {argv[1]!r}") from e
    call = _read_line(path, line_no)
    render(call, Console())


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ViewError as e:
        print(f"错误：{e}", file=sys.stderr)
        raise SystemExit(1) from e
