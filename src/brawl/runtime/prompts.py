"""跨玩法的 human 消息拼装（游戏盲）。

一条 human 消息 = 公开事件块（可空）+ 局面/私密信息（可空）+ 风格提醒 + 提问。
各玩法只负责提供这四个部件（事件 / 局面 / 提问文案都是玩法语义），拼装纪律收在这里：

- 风格提醒每轮重申：人设在 system 开头会被长局历史稀释，放到距生成位置近的尾部对冲；
  但提问必须压轴——格式锚（JSON 形状 / 标签行）是最末位的锚，实测把提醒放在提问之后
  会顶掉格式锚，模型照着提醒的括号体例回话、不出格式。tone 为空时退化成纯风格提醒，零分叉。
- mimicry_guard：对局中默认开（反从众，挡长局风格塌缩——别被前人句式带跑）；赛后复盘
  关掉，那里恰恰要互相接话（接梗/回怼/帮腔），只保留"用自己的腔调"、不再叫人绕开前面发言者。
"""

from __future__ import annotations

_STYLE_DISCIPLINE = "说话简短干练，只打 1~2 个重点，别写长段总结，别复述全桌。"


def human_message(events: list[str], context: str, ask: str, tone: str = "", mimicry_guard: bool = True) -> str:
    """公开事件块（可空）+ 局面/私密信息（可空）+ 风格提醒 + 提问 → 一条 human 消息。"""
    parts = []
    if events:
        parts.append("场上发生：\n" + "\n".join(events))
    if context:
        parts.append(context)
    guard = "别被前面发言者的句式和开场带着走" if mimicry_guard else "该接梗接梗、该回怼回怼，别自说自话"
    parts.append(f"（风格提醒：{tone}保持你自己的腔调，{_STYLE_DISCIPLINE}{guard}。）")
    parts.append(ask)
    return "\n\n".join(parts)
