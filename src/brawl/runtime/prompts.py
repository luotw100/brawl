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

from brawl.runtime.types import Delivery, Seat

_STYLE_DISCIPLINE = "说话简短干练，只打 1~2 个重点，别写长段总结，别复述全桌。"


def attribution_line(speaker: Seat, delivery: Delivery) -> str:
    """一条 delivery 写进席位 inbox 时的最终文本。

    narration 是完整成句，原样入箱；speech 是裸发言，在此包归属——归属只服务于
    LLM inbox（让它们分辨谁说了什么），TTS/字幕等其余消费方拿裸 `text`。
    """
    if delivery.kind == "narration":
        return delivery.text
    label = delivery.attribution or f"{speaker}号"
    return f"{label}：「{delivery.text}」"


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
