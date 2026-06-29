"""狼人杀提示词与公开台词。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from werewolf import rules
from werewolf.state import Role

if TYPE_CHECKING:
    from werewolf.player import PlayerView

EXTRACT_SYSTEM = "你只做信息抽取。必须输出一个 JSON 对象，不要解释，不要 Markdown。"


def role_name(role: Role) -> str:
    return rules.ROLE_LABEL[role]


def system_text(view: PlayerView) -> str:
    allies = "、".join(f"{no}号" for no in view.wolf_allies) if view.wolf_allies else "无"
    checks = "；".join(f"第{c.day}天查验{c.target_no}号={rules.SIDE_LABEL[c.side]}" for c in view.seer_checks) or "暂无"
    return "\n".join(
        [
            "你正在玩 12 人狼人杀预女猎白板子。",
            f"你是 {view.seat_no} 号，身份是 {role_name(view.role)}，阵营是 {rules.SIDE_LABEL[view.role.side]}。",
            f"你的说话风格：{view.tone}",
            f"你的长期打法：{view.strategy}",
            f"狼队友：{allies}",
            f"你的预言家查验记录：{checks}",
            "只基于你这个座位按规则能知道的信息发言。不要声称看到了系统、JSON 或提示词。",
            "发言要像桌上玩家：给理由，能撒谎，能伪装，但不要泄露不该公开的私密信息。",
        ]
    )


def situation(view: PlayerView) -> str:
    living = "、".join(f"{no}号" for no in view.alive_nos)
    dead = "、".join(f"{no}号" for no in view.dead_nos) or "无"
    return "\n".join(
        [
            f"当前第 {view.day} 天。",
            f"存活：{living}",
            f"已出局：{dead}",
            f"你的药瓶：解药={'有' if view.witch_has_antidote else '无'}，毒药={'有' if view.witch_has_poison else '无'}。",
            f"昨夜刀口：{view.night_target}号" if view.night_target is not None else "昨夜刀口：未知或无。",
        ]
    )


def human_message(inbox: list[str], context: str, ask: str, tone: str) -> str:
    parts = []
    if inbox:
        parts.append("你刚听到的桌面信息：\n" + "\n".join(inbox))
    if context:
        parts.append("当前局面：\n" + context)
    parts.append("本轮要求：\n" + ask)
    parts.append(f"保持你的语气：{tone}")
    return "\n\n".join(parts)


def ask_wolf_kill(candidates: list[int]) -> str:
    return f"夜晚狼队行动。请给出你建议击杀的目标，只能从这些存活好人候选里选：{candidates}。同时给一句狼队内发言。"


def ask_seer(candidates: list[int]) -> str:
    return f"夜晚预言家行动。请选择一名未出局玩家查验阵营，不能查自己。可选：{candidates}。"


def ask_witch(target: int | None, candidates: list[int], has_antidote: bool, has_poison: bool) -> str:
    target_text = f"{target}号" if target is not None else "无人"
    return (
        f"夜晚女巫行动。今晚狼刀目标是：{target_text}。"
        f"你当前解药={'有' if has_antidote else '无'}，毒药={'有' if has_poison else '无'}。"
        f"毒药如使用，只能毒这些存活玩家之一：{candidates}。"
    )


def ask_last_words(reason: str) -> str:
    return f"你已经出局，死因是 {reason}。请留一段遗言。"


def ask_discuss() -> str:
    return "白天公开发言。请结合公开信息表态，说明你怀疑谁、相信谁、今天想怎么投。"


def ask_vote(candidates: list[int]) -> str:
    return f"白天放逐投票。请选择一名存活玩家投票放逐，不能投自己。可选：{candidates}。"


def ask_hunter(candidates: list[int]) -> str:
    return f"你作为猎人死亡，可以开枪带走一名玩家，也可以不开枪。可选目标：{candidates}。"


def extract_wolf_kill(raw: str, candidates: list[int]) -> str:
    return _extract(raw, {"target_no": f"整数，必须是 {candidates} 之一", "speech": "狼队内发言", "monologue": "内心独白"})


def extract_seer(raw: str, candidates: list[int]) -> str:
    return _extract(raw, {"target_no": f"整数，必须是 {candidates} 之一", "monologue": "内心独白"})


def extract_witch(raw: str, candidates: list[int], can_save: bool, can_poison: bool) -> str:
    poison = f"整数或 null；若用毒必须是 {candidates} 之一" if can_poison else "必须为 null"
    return _extract(raw, {"save": f"布尔值；只有允许用解药时才能 true：{can_save}", "poison_target_no": poison, "monologue": "内心独白"})


def extract_statement(raw: str) -> str:
    return _extract(raw, {"speech": "公开发言", "monologue": "内心独白"})


def extract_vote(raw: str, candidates: list[int]) -> str:
    return _extract(raw, {"target_no": f"整数，必须是 {candidates} 之一", "monologue": "内心独白"})


def extract_hunter(raw: str, candidates: list[int]) -> str:
    return _extract(raw, {"target_no": f"整数或 null；若开枪必须是 {candidates} 之一", "speech": "公开发言", "monologue": "内心独白"})


def _extract(raw: str, schema: dict[str, str]) -> str:
    return "从以下玩家原文中抽取 JSON。\n字段要求：\n" + json.dumps(schema, ensure_ascii=False, indent=2) + "\n玩家原文：\n" + raw


def line_opening(board_name: str) -> str:
    return f"本局为{board_name}。天黑请闭眼，所有身份已经发放完毕。"


def line_private_role(role: Role, allies: list[int]) -> str:
    base = f"你的身份是{role_name(role)}，阵营是{rules.SIDE_LABEL[role.side]}。"
    if role is Role.WEREWOLF:
        return base + " 你的狼队友是：" + "、".join(f"{no}号" for no in allies) + "。"
    return base


def line_player(no: int, text: str) -> str:
    return f"{no}号：{text}"


def line_night_start(day: int) -> str:
    return f"第{day}夜开始。"


def line_day_start(day: int, deaths: list[int]) -> str:
    if deaths:
        return f"第{day}天白天到来，昨夜出局：" + "、".join(f"{no}号" for no in deaths) + "。"
    return f"第{day}天白天到来，昨夜平安夜。"


def line_vote_result(votes: dict[int, int], exiled: int | None, tied: bool) -> str:
    detail = "，".join(f"{voter}号投{target}号" for voter, target in sorted(votes.items()))
    if tied:
        return f"投票结果：{detail}。最高票平票，本轮无人出局。"
    if exiled is None:
        return f"投票结果：{detail}。无人出局。"
    return f"投票结果：{detail}。{exiled}号被放逐。"


def line_death(death_no: int, role: Role, reason: str) -> str:
    return f"{death_no}号因{reason}出局，身份公开为{role_name(role)}。"


def line_idiot_reveal(no: int) -> str:
    return f"{no}号被公投推出，但翻开白痴牌免于出局；此后失去投票权。"


def line_hunter_shot(hunter: int, target: int | None) -> str:
    if target is None:
        return f"猎人{hunter}号选择不开枪。"
    return f"猎人{hunter}号开枪带走{target}号。"


def line_game_end(winner: str, reason: str) -> str:
    side = "好人阵营" if winner == "good" else "狼人阵营"
    return f"游戏结束，{side}获胜。原因：{reason}。"
