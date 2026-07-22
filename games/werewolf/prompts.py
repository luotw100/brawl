"""提示词构造：所有发给模型的文案集中在此（纯函数，不引模板引擎），与游戏逻辑分离。

铁律：只告诉 AI 它这个座位按规则能知道的——自己的角色、（狼人的）同伴名单、（预言家的）
查验历史、（女巫的）药瓶与今夜刀口。别人的身份、谁投了谁的内心、谁是真预言家一律不给，
让它从发言、票型、死亡翻牌这些明牌里自己推。

消息结构：first_speak 时 rules.view 产出含【固定设定】的全量 prompt（规则、板子、座号、
角色与发牌时的同伴名单、打法取向）；此后每次行动只带增量（inbox 公开事件 + 当前局面 +
该席位私密情报 + 风格提醒 + JSON 提问）。会随时间增长的私密信息（预言家逐夜累积的查验史、
女巫药瓶/刀口）一律放进每轮 human，绝不进固定设定（否则 opencode session 前缀变动、缓存全失）。

发给模型的文本一律一句一行写满、句中绝不折行——源码里为行宽手动折行的换行会原样进入 API
消息、把句子从中间撕开。段落间的空行（\\n\\n）是刻意分隔，保留。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from werewolf import rules
from werewolf.rules import Role

if TYPE_CHECKING:
    from werewolf.view import PlayerView


RULES = """\
你正在玩桌游《狼人杀》，12 人预女猎白板子，这是一场直播，有观众在看。

阵营与角色：
- 狼人阵营：4 个狼人。好人阵营：4 个神职（预言家、女巫、猎人、白痴）+ 4 个平民。
- 狼人夜里睁眼互认、共同决定刀一个人；预言家每夜查验一名玩家的真实阵营（只有自己知道）；女巫有一瓶解药（可自救）和一瓶毒药，各只能用一次，同夜可对不同目标同时用两瓶药；猎人被狼刀或被投票放逐出局时可以开枪，纯粹被女巫毒死则不能开枪，同夜既中狼刀又中毒仍按狼刀触发；白痴被投票放逐时翻牌免死、留在场上但从此失去投票权；平民没有任何技能。

流程：
- 每天分黑夜与白天。黑夜：狼人共商刀口 → 预言家验人 → 女巫决定救/毒。天亮后公布昨夜死讯并翻开死者身份，死者留遗言。
- 白天：全体存活玩家轮流发言，然后全体同时暗投票放逐一人（票型开出后公开，平票则无人出局）。被放逐者翻牌、留遗言。
- 胜负（屠边）：狼人全部出局 → 好人胜；4 个神职全部出局，或 4 个平民全部出局 → 狼人胜。若同一次结算同时达成狼全灭与神/民全灭，好人胜优先。

对局方式：
- 我会持续把桌面上公开发生的事播报给你，并在轮到你行动时向你提问。
- 你的每次作答必须恰好是一个 JSON 对象，首尾空白外不能有推理文字、代码围栏或第二个对象：monologue 是给观众的一句内心短话（场上玩家看不到），speech 是当前阶段要求的发言，其余字段按提问要求给出。
- speech 的可见范围由阶段决定：night_wolf 的 speech 只在狼队内部传递，可以真实讨论刀口；day_discuss、last_words、hunter_shot 的 speech 会一字不差公布上桌，可以按阵营利益策略性跳身份、报情报或伪装；banter 是赛后公开复盘。monologue 始终只给观众看，场上玩家看不到。

发言要求（观众在看，桌上交锋是节目的全部看点）：
- speech 要言之有物：口语化的 2~4 句，摆出具体论据——点名座位号、引用谁的发言或票型、结合昨夜死讯，别只丢一句「我是好人」「跟上一个走」。
- 「相信好人」「狼人很狡猾」这类不点名、不给事实的万金油等于没说；前面有人说过的论点别原样复读，要么补新论据，要么正面反驳他。
- 预言家可以跳身份报查验、狼人可以悍跳预言家或装好人、神民可以站边带节奏——大胆对跳、对踩、扛推，比和稀泥好看得多。
- monologue 是给观众的吐槽：一句话，要有情绪、可以毒舌、可以自嘲，别干巴巴复述计划。"""

_REASONING_BASELINE = "推理基准：主持人播报的公开事实和你自己的私密视角才是确定事实；其他玩家的任何说法，包括身份声明、验人报料、遗言指认，都只是玩家声称，可能为真也可能为假；可以参考，但不能当作事实前提。"
_STYLE_DISCIPLINE = "说话简短干练，只打 1~2 个重点，别写长段总结，别复述全桌。"


def role_name(role: Role) -> str:
    return rules.ROLE_LABEL[role]


# ---------------------------------------------------------------- 固定设定（发牌时刻固定）

def _role_brief(view: PlayerView) -> str:
    """该席位的角色说明：只陈述角色、阵营、技能与发牌时的同伴名单，不给打法建议。

    随时间增长的私密信息（预言家查验史、女巫药瓶）不在这里，进每轮 human（见 private_block）。"""
    if view.role is Role.WEREWOLF:
        allies = "、".join(f"{no}号" for no in view.wolf_allies) if view.wolf_allies else "无（你是独狼）"
        return f"你的角色：狼人（狼人阵营）。你的狼队友是：{allies}。夜里你们一起商量刀谁；白天你要装成好人，可以悍跳预言家、误导投票。"
    if view.role is Role.SEER:
        return "你的角色：预言家（好人阵营·神）。每天夜里你可以查验一名玩家，得知其真实阵营（好人或狼人），只有你知道。你的查验记录会在每次行动时一并给你。"
    if view.role is Role.WITCH:
        return "你的角色：女巫（好人阵营·神）。你有一瓶解药（可救当晚被狼刀的人）和一瓶毒药（可毒死一人），各只能用一次。每夜你会被告知今晚的刀口与药瓶剩余。"
    if view.role is Role.HUNTER:
        return "你的角色：猎人（好人阵营·神）。当你被狼刀或被投票放逐出局时，可以开枪带走场上一名玩家；但若你是被女巫毒死的，则不能开枪。"
    if view.role is Role.IDIOT:
        return "你的角色：白痴（好人阵营·神）。若你被投票放逐，会当场翻牌免于出局、继续留在场上发言，但从此失去投票权。被狼刀或被毒则正常出局。"
    return "你的角色：平民（好人阵营）。你没有任何夜晚技能，只能靠白天的发言与投票找出狼人。"


def system_text(view: PlayerView) -> str:
    """整局固定的设定：规则 + 本局板子人数 + 该席位身份与发牌时同伴。整局逐字不变（缓存前缀）。"""
    board = rules.PRE_WITCH_HUNTER_IDIOT_12
    return (
        f"你是一名《狼人杀》玩家。{view.tone}\n\n"
        f"{RULES}\n\n"
        f"本局是{board.name}，共 {view.players} 人，座位号 1~{view.players}。\n\n"
        f"你是 {view.seat_no} 号。{_role_brief(view)}\n\n"
        f"你的打法取向：{view.strategy}"
    )


# ---------------------------------------------------------------- human 消息拼装

def situation(view: PlayerView) -> str:
    """当前公开局面：第几天、谁存活、谁出局 + 公开账本（死亡翻牌、放逐票型）。

    账本全是明牌；冷续跑还会从 sentence journal 重建该席位完整的私有对话历史。"""
    living = "、".join(f"{no}号" for no in view.alive_nos)
    dead = "、".join(f"{no}号" for no in view.dead_nos) or "无"
    parts = [f"当前第 {view.day} 天。存活：{living}。已出局：{dead}。"]
    if view.deaths:
        rows = "；".join(
            f"第{d.day}天 {d.seat_no}号 出局（翻牌：{role_name(d.role)}）"
            for d in view.deaths
        )
        parts.append(f"死亡翻牌：{rows}。")
    if view.votes_history:
        rows = []
        for v in view.votes_history:
            marks = "，".join(f"{a}号→{b}号" for a, b in sorted(v.votes.items()))
            if v.tied:
                outcome = "平票无人出局"
            elif v.exiled is None:
                outcome = f"{v.top_voted}号 得票最高但未被放逐"
            else:
                outcome = f"{v.exiled}号 被放逐"
            rows.append(f"第{v.day}天 票型[{marks}]→{outcome}")
        parts.append("放逐记录：\n" + "\n".join(rows))
    parts.append(_REASONING_BASELINE)
    return "\n".join(parts)


def private_block(view: PlayerView) -> str:
    """该席位随时间增长的私密情报（预言家查验史；女巫药瓶与今夜刀口；其余角色为空，零分叉拼接）。"""
    parts: list[str] = []
    if view.role is Role.SEER and view.seer_checks:
        checks = "；".join(f"第{c.day}夜验{c.target_no}号={rules.SIDE_LABEL[c.side]}" for c in view.seer_checks)
        parts.append(f"（只有你知道）你的查验记录：{checks}。")
    if view.role is Role.WITCH:
        antidote = "可用" if view.witch_has_antidote else "已用完"
        poison = "可用" if view.witch_has_poison else "已用完"
        parts.append(f"（只有你知道）你的药瓶：解药{antidote}，毒药{poison}。")
    return "\n".join(parts)


def human_message(events: list[str], context: str, ask: str, tone: str = "", mimicry_guard: bool = True) -> str:
    """一条 human 消息 = 公开事件块（可空）+ 局面/私密信息（可空）+ 风格提醒 + 提问。

    风格提醒每轮重申、贴近生成位置，对冲长局里开头腔调被历史发言稀释；提问必须压轴
    （JSON 格式锚要在最末位）。mimicry_guard 对局中默认开（反从众）；赛后复盘关掉，
    让他们接梗回怼。"""
    parts = []
    if events:
        parts.append("场上发生：\n" + "\n".join(events))
    if context:
        parts.append(context)
    guard = "别被前面发言者的句式和开场带着走" if mimicry_guard else "该接梗接梗、该回怼回怼，别自说自话"
    parts.append(f"（风格提醒：{tone}保持你自己的腔调，{_STYLE_DISCIPLINE}{guard}。）")
    parts.append(ask)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- OpenCode agent 专用提问（JSON schema）

_OUTPUT_ONLY = "只输出上述 JSON 对象，不要输出其他文字。"


def opencode_ask(phase: str, view: PlayerView) -> str:
    """为 OpenCode agent 生成当前阶段的 JSON 输出要求。"""
    cands = "、".join(f"{no}号" for no in view.candidates)
    if phase == "night_wolf":
        return (
            "当前阶段：夜晚狼人行动（night_wolf）。和狼队友商量今晚刀谁——如果上面有队友今夜的提议，先回应（附议或提出别的刀口）再给结论。\n"
            f"可选刀口：{cands}。最终刀口取全体存活狼人提议里的多数（平票取最小座位号）。\n"
            f"输出 JSON：\n"
            f'{{"phase": "wolf_kill", "monologue": "说给观众的一句短话", "speech": "对狼队友说的一句话（只有狼队听得到）", "target_no": 座位号}}\n'
            f"{_OUTPUT_ONLY}"
        )
    if phase == "night_seer":
        return (
            f"当前阶段：夜晚预言家行动（night_seer）。选一名玩家查验真实阵营（结果只有你知道）。可选：{cands}。\n"
            f"输出 JSON：\n"
            f'{{"phase": "seer_check", "monologue": "说给观众的一句短话", "target_no": 座位号}}\n'
            f"{_OUTPUT_ONLY}"
        )
    if phase == "night_witch":
        kill = f"{view.night_target}号" if view.night_target is not None else "无人（今夜空刀）"
        if view.witch_has_antidote and view.night_target is not None:
            antidote = "解药可用；save 可以为 true（救刀口）或 false（不用解药）。"
        elif view.witch_has_antidote:
            antidote = "解药尚在，但今夜没有刀口；save 必须为 false。"
        else:
            antidote = "解药已用完；save 必须为 false。"
        poison = (
            f"毒药可用；poison_target_no 可以从 {cands} 中选一人，或填 null（不用毒药）。"
            if view.witch_has_poison
            else "毒药已用完；poison_target_no 必须为 null。"
        )
        return (
            f"当前阶段：夜晚女巫行动（night_witch）。今晚狼人的刀口是：{kill}。\n"
            f"{antidote}\n{poison}\n"
            f"输出 JSON：\n"
            f'{{"phase": "witch_action", "monologue": "说给观众的一句短话", "save": true或false, "poison_target_no": 座位号或null}}\n'
            f"{_OUTPUT_ONLY}"
        )
    if phase == "last_words":
        return (
            f"当前阶段：遗言（last_words）。你已经出局，现在是你的遗言时间，全场会听到。\n"
            f"输出 JSON：\n"
            f'{{"phase": "last_words", "monologue": "说给观众的一句短话", "speech": "你的遗言，会一字不差公布上桌"}}\n'
            f"{_OUTPUT_ONLY}"
        )
    if phase == "day_discuss":
        return (
            "当前阶段：白天自由发言（day_discuss）。结合昨夜死讯、前面玩家的发言和票型，亮明你的立场。\n"
            f"输出 JSON：\n"
            f'{{"phase": "discuss", "monologue": "说给观众的一句短话", "speech": "你的桌面发言，会一字不差公布上桌"}}\n'
            f"{_OUTPUT_ONLY}"
        )
    if phase == "day_vote":
        return (
            f"当前阶段：放逐投票（day_vote）。全员同时暗投，你看不到别人这一票；开票后票型公开，平票无人出局。可选：{cands}。\n"
            f"输出 JSON：\n"
            f'{{"phase": "vote", "monologue": "说给观众的一句短话", "target_no": 座位号}}\n'
            f"{_OUTPUT_ONLY}"
        )
    if phase == "hunter_shot":
        return (
            f"当前阶段：猎人开枪（hunter_shot）。你作为猎人出局，可以开枪带走场上一名玩家，也可以不开枪。可选目标：{cands}。\n"
            f"输出 JSON：\n"
            f'{{"phase": "hunter_shot", "monologue": "说给观众的一句短话", "speech": "开枪前的一句话，会一字不差公布上桌", "target_no": 座位号或null}}\n'
            f"{_OUTPUT_ONLY}"
        )
    if phase == "banter":
        return (
            "当前阶段：赛后复盘（banter）。游戏已结束，底牌已翻开，输赢已定，畅所欲言。\n"
            f"输出 JSON：\n"
            f'{{"phase": "banter", "monologue": "说给观众的一句短话", "speech": "你的复盘发言"}}\n'
            f"{_OUTPUT_ONLY}"
        )
    raise ValueError(f"未知的 OpenCode 决策阶段：{phase}")


def opencode_prompt(view: PlayerView, *, include_context: bool = True) -> str:
    """OpenCode 会话里发送的自然传话文本（first_speak 时带全量设定，否则只带增量）。"""
    if not include_context:
        return (
            f"主持人继续通过你的私人频道给你传话。沿用本 session 开局时给你的身份、夜晚情报、角色约束、"
            f"打法取向和私人历史；现在只处理下面的新公开事件和当前行动，不要重述旧信息。\n\n"
            f"【现在轮到你】\n{view.prompt}"
        )
    return (
        f"我是这局《狼人杀》的主持人，现在通过你的私人频道给你传话。你只代表自己这个座位行动，"
        f"其他玩家看不到这条私信，也看不到你的 monologue。\n\n"
        f"【你的固定设定】\n{system_text(view)}\n\n"
        f"【你的私人历史】\n{_private_transcript(view)}\n\n"
        f"【现在轮到你】\n{view.prompt}"
    )


def _private_transcript(view: PlayerView) -> str:
    """把该席位已提交的历史线程转成自然转述，不暴露内部 role 标签。"""
    if not view.messages:
        return "你此前还没有私人行动记录。"
    rows = []
    for message in view.messages:
        prefix = "主持人此前对你说" if message.role == "human" else "你此前回复"
        rows.append(f"{prefix}：\n{message.content}")
    return "你的私人行动记录：\n" + "\n\n".join(rows)


# ---------------------------------------------------------------- 公开事件行
#
# line_host_*：主持人台词，经 host 句子同时进全员 inbox 与观众流。主持人控场串场——宣布
# 天黑天亮、死讯与翻牌、放逐结果、终局身份——绝不复述玩家发言，口语可读、不带念不出来的符号。
# line_player：玩家发言原文上桌的广播行，进全员 inbox。
# line_wolf_chat：狼队夜聊，只进**狼席位** inbox 与观众流（好人永远看不到）。
# line_vote_detail / line_hunter_shot / line_idiot_immune：公开账本明细，进全员 inbox。

def line_player(no: int, speech: str) -> str:
    return f"{no}号：「{speech}」"


def line_wolf_chat(no: int, target: int, speech: str) -> str:
    return f"狼队 {no}号（提议刀{target}号）：「{speech}」"


def line_vote_detail(votes: dict[int, int]) -> str:
    marks = "，".join(f"{voter}号→{target}号" for voter, target in sorted(votes.items()))
    return f"【放逐票型】{marks}"


def line_idiot_immune(no: int) -> str:
    return f"{no}号 是已翻牌的白痴，放逐对其无效，本轮无人出局。"


def line_hunter_shot(hunter: int, target: int | None, role: Role | None) -> str:
    if target is None or role is None:
        return f"猎人 {hunter}号 选择不开枪。"
    return f"猎人 {hunter}号 开枪带走 {target}号，其身份是{role_name(role)}。"


def line_host_opening(board_name: str, players: int, wolves: int) -> str:
    return f"欢迎来到《狼人杀》，{board_name}，{players}人局，其中 {wolves} 只狼。身份已经发放完毕。"


def line_host_night(day: int) -> str:
    return f"第{day}天，天黑请闭眼。"


def line_host_dawn(day: int, deaths: list[tuple[int, Role]]) -> str:
    if not deaths:
        return f"第{day}天，天亮了。昨夜是平安夜，无人出局。"
    who = "、".join(f"{no}号" for no, _ in deaths)
    ids = "，".join(f"{no}号的身份是{role_name(role)}" for no, role in deaths)
    return f"第{day}天，天亮了。昨夜倒下的是 {who}。{ids}。"


def line_host_vote_result(top_voted: int | None, role: Role | None, tied: bool) -> str:
    if tied:
        return "投票结束，最高票平票，本轮无人被放逐。"
    if top_voted is None or role is None:
        return "投票结束，本轮无人被放逐。"
    if role is Role.IDIOT:
        # 白痴翻牌免死的局：只说票型，"出局"与否由紧随其后的白痴播报交代。
        return f"投票结束，{top_voted}号 得票最高。"
    return f"投票结束，{top_voted}号 被放逐出局，其身份是{role_name(role)}。"


def line_host_idiot(no: int) -> str:
    return f"投票结束，{no}号 被推上，但他翻开了白痴牌，免于放逐、留在场上，此后不能再投票。"


def line_host_reveal(winner: str, win_reason: str, roles: dict[int, Role]) -> str:
    side = rules.SIDE_LABEL[rules.Side(winner)] if winner in (s.value for s in rules.Side) else winner
    reason = rules.WIN_REASON_LABEL.get(win_reason, win_reason)
    pairs = "、".join(f"{no}号是{role_name(r)}" for no, r in sorted(roles.items()))
    return f"游戏结束，{side}获胜（{reason}）。身份公布：{pairs}。开始复盘。"


def line_host_closing() -> str:
    return "本局结束，下一局见。"
