"""提示词构造：所有发给模型的文案集中在此（纯函数，不引模板引擎），与游戏逻辑分离。

铁律：只告诉 AI 它这个座位按规则能知道的——自己的角色、（狼人的）同伴名单、（预言家的）
查验历史、（女巫的）药瓶与今夜刀口。别人的身份、谁投了谁的内心、谁是真预言家一律不给，
让它从发言、票型、死亡翻牌这些明牌里自己推。

消息结构（吃 prompt 缓存）：system 装"发牌时刻就固定的全部"（规则、本局角色阵容、人数座次、
自己座号、角色与发牌时的同伴名单），整局逐字不变；此后每次行动是一条 human（自上次行动以来
进 inbox 的公开事件 + 当前局面 + 该席位的私密情报 + 风格提醒 + 提问）。会随时间增长的私密
信息（预言家逐夜累积的查验史、女巫药瓶/刀口）一律放进 human，绝不进 system（否则 system
变动、缓存全失）。

模型自由文本作答（不写 JSON），最后用标签行收尾（「发言：」「刀人：」「投票：」……）。
公开/私密边界靠标签行：「发言：」内容一字不差上桌，标签外的自由发挥与「内心戏：」只在自己
线程与观众侧，绝不上桌。结构化解析交给固定轻量模型 extractor（见文件末尾）。

发给模型的文本一律一句一行写满、句中绝不折行——源码里为行宽手动折行的换行会原样进入 API
消息、把句子从中间撕开。段落间的空行（\\n\\n）是刻意分隔，保留。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from werewolf import rules
from werewolf.rules import Role

if TYPE_CHECKING:
    from werewolf.player import PlayerView


RULES = """\
你正在玩桌游《狼人杀》，12 人预女猎白板子，这是一场直播，有观众在看。

阵营与角色：
- 狼人阵营：4 个狼人。好人阵营：4 个神职（预言家、女巫、猎人、白痴）+ 4 个平民。
- 狼人夜里睁眼互认、共同决定刀一个人；预言家每夜查验一名玩家的真实阵营（只有自己知道）；女巫有一瓶解药（救当晚被刀的人）和一瓶毒药（毒死一人），各只能用一次；猎人被狼刀或被投票放逐出局时可以开枪带走一名玩家（但被女巫毒死则不能开枪）；白痴被投票放逐时翻牌免死、留在场上但从此失去投票权；平民没有任何技能。

流程：
- 每天分黑夜与白天。黑夜：狼人共商刀口 → 预言家验人 → 女巫决定救/毒。天亮后公布昨夜死讯并翻开死者身份，死者留遗言。
- 白天：全体存活玩家轮流发言，然后全体同时暗投票放逐一人（票型开出后公开，平票则无人出局）。被放逐者翻牌、留遗言。
- 胜负（屠边）：狼人全部出局 → 好人胜；4 个神职全部出局，或 4 个平民全部出局 → 狼人胜。

对局方式：
- 我会持续把桌面上公开发生的事播报给你，并在轮到你行动时向你提问。
- 作答时你可以先自由思考、随便写，但回复的最后必须按提问要求给出标签行（如「发言：」「投票：」），每个标签独占一行。
- 「发言：」行的内容会被一字不差地公布上桌，全场可见，所以你的真实身份、查验结果、刀口计划绝不要写进「发言：」行（除非你故意跳身份）。
- 「内心戏：」行是说给观众听的一句短话，场上玩家看不到；标签行之外的自由发挥同样不上桌，只有你自己下次行动时还能看到。

发言要求（观众在看，桌上交锋是节目的全部看点）：
- 「发言：」要言之有物：口语化的 2~4 句，摆出具体论据——点名座位号、引用谁的发言或票型、结合昨夜死讯，别只丢一句「我是好人」「跟上一个走」。
- 「相信好人」「狼人很狡猾」这类不点名、不给事实的万金油等于没说；前面有人说过的论点别原样复读，要么补新论据，要么正面反驳他。
- 预言家可以跳身份报查验、狼人可以悍跳预言家或装好人、神民可以站边带节奏——大胆对跳、对踩、扛推，比和稀泥好看得多。
- 「内心戏：」是给观众的吐槽：一句话，要有情绪、可以毒舌、可以自嘲，别干巴巴复述计划。"""


def role_name(role: Role) -> str:
    return rules.ROLE_LABEL[role]


# ---------------------------------------------------------------- system（发牌时刻固定）

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
    """整局固定的 system：规则 + 本局人数 + 该席位身份与发牌时同伴。整局逐字不变（缓存前缀）。"""
    return (
        f"你是一名《狼人杀》玩家。{view.tone}\n\n"
        f"{RULES}\n\n"
        f"本局共 {view.players} 人，座位号 1~{view.players}。\n\n"
        f"你是 {view.seat_no} 号。{_role_brief(view)}\n\n"
        f"你的打法取向：{view.strategy}"
    )


# ---------------------------------------------------------------- human 消息拼装

def situation(view: PlayerView) -> str:
    """当前公开局面：第几天、谁存活、谁出局。全是明牌。"""
    living = "、".join(f"{no}号" for no in view.alive_nos)
    dead = "、".join(f"{no}号" for no in view.dead_nos) or "无"
    return f"当前第 {view.day} 天。存活：{living}。已出局：{dead}。"


def private_block(view: PlayerView) -> str:
    """该席位随时间增长的私密情报（只预言家有查验史；其余角色为空，零分叉拼接）。"""
    if view.role is Role.SEER and view.seer_checks:
        checks = "；".join(f"第{c.day}夜验{c.target_no}号={rules.SIDE_LABEL[c.side]}" for c in view.seer_checks)
        return f"（只有你知道）你的查验记录：{checks}。"
    return ""


def human_message(events: list[str], context: str, ask: str, tone: str, mimicry_guard: bool = True) -> str:
    """一条 human 消息 = 公开事件块（可空）+ 局面/私密信息（可空）+ 风格提醒 + 提问。

    风格提醒每轮重申、贴近生成位置，对冲长局里 system 开头腔调被历史发言稀释；提问必须压轴
    （标签行格式锚要在最末位）。mimicry_guard 对局中默认开（反从众）；赛后复盘关掉，让他们接梗回怼。"""
    parts = []
    if events:
        parts.append("场上发生：\n" + "\n".join(events))
    if context:
        parts.append(context)
    guard = "别被前面发言者的句式和开场带着走" if mimicry_guard else "该接梗接梗、该回怼回怼，别自说自话"
    parts.append(f"（风格提醒：{tone}保持你自己的腔调，{guard}。）")
    parts.append(ask)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 各决策提问（标签行收尾）

def ask_wolf_kill(candidates: list[int], prior: list[tuple[int, int, str]]) -> str:
    """狼人提刀。prior = 本夜已发言狼队友的 (座位号, 提议刀口, 夜聊)，按发言序；空 = 你是头狼先说。
    最终刀口取全体狼人提议里的多数（平则取最小座位号），所以先说的人能带节奏。"""
    cands = "、".join(f"{no}号" for no in candidates)
    chat = ""
    if prior:
        lines = "\n".join(f"{no}号（提议刀{tgt}号）：「{speech}」" for no, tgt, speech in prior)
        chat = "今夜你的狼队友已经说了：\n" + lines + "\n你可以附议、也可以提不同的刀口，先回应再给结论。\n"
    return (
        "天黑了，狼人请行动。和狼队友商量今晚刀谁——目标只能是场上存活的玩家（也可以刀你自己人来伪装，但通常不会）。\n"
        + chat +
        f"可选刀口：{cands}。\n"
        f"回复最后必须是这三行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"发言：<对狼队友说的一句话，只有狼队友听得到>\n"
        f"刀人：<一个座位号>"
    )


def ask_seer(candidates: list[int]) -> str:
    cands = "、".join(f"{no}号" for no in candidates)
    return (
        f"夜晚预言家行动。选一名玩家查验真实阵营（不能查自己，结果只有你知道）。可选：{cands}。\n"
        f"回复最后必须是这两行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"查验：<一个座位号>"
    )


def ask_witch(target: int | None, candidates: list[int], has_antidote: bool, has_poison: bool) -> str:
    kill = f"{target}号" if target is not None else "无人（今夜空刀或平安）"
    antidote = "可用" if has_antidote else "已用完"
    poison = "可用" if has_poison else "已用完"
    cands = "、".join(f"{no}号" for no in candidates)
    save_rule = "今晚有刀口且你解药可用，才能救" if (has_antidote and target is not None) else "（本回合不能用解药）"
    poison_rule = f"若用毒只能毒这些存活玩家之一：{cands}" if has_poison else "（毒药已用完，只能填 不用）"
    return (
        f"夜晚女巫行动。今晚狼人的刀口是：{kill}。你的解药：{antidote}；毒药：{poison}。\n"
        f"你可以用解药救回刀口、用毒药毒死一人、或都不用（同一晚不要对同一个人又救又毒）。\n"
        f"解药规则：{save_rule}。毒药规则：{poison_rule}。\n"
        f"回复最后必须是这三行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"救人：<是 或 否>\n"
        f"用毒：<一个座位号，或 不用>"
    )


def ask_last_words(reason: str) -> str:
    return (
        f"你已经出局（{rules.DEATH_REASON_LABEL.get(reason, reason)}），现在是你的遗言时间，全场会听到。\n"
        f"你可以盘点局势、报你的身份与情报、指认你怀疑的狼、或给好人留方向——也可以继续伪装。\n"
        f"回复最后必须是这两行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"发言：<你的遗言，会一字不差公布上桌>"
    )


def ask_discuss() -> str:
    return (
        "白天自由发言轮，轮到你了。结合昨夜死讯、前面玩家的发言和票型，亮明你的立场：你怀疑谁、相信谁、今天想投谁，给出理由。\n"
        "回复最后必须是这两行：\n"
        "内心戏：<说给观众的一句短话>\n"
        "发言：<你的桌面发言，会一字不差公布上桌>"
    )


def ask_vote(candidates: list[int]) -> str:
    cands = "、".join(f"{no}号" for no in candidates)
    return (
        f"白天放逐投票，全员同时投，你看不到别人这一票投谁，但开票后所有人的票都会公开。选一名存活玩家放逐（不能投自己）。可选：{cands}。\n"
        f"回复最后必须是这两行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"投票：<一个座位号>"
    )


def ask_hunter(candidates: list[int]) -> str:
    cands = "、".join(f"{no}号" for no in candidates)
    return (
        f"你作为猎人出局，现在可以开枪带走场上一名玩家，也可以选择不开枪。可选目标：{cands}。\n"
        f"回复最后必须是这三行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"发言：<开枪前的一句话，会一字不差公布上桌>\n"
        f"开枪：<一个座位号，或 不开枪>"
    )


def ask_banter(prior: list[tuple[int, str]]) -> str:
    """赛后复盘提问。prior = 本环节已发言者的 (座位号, 发言)，按发言序；空 = 你是第一个复盘的。"""
    roundtable = ""
    if prior:
        recap = "\n".join(line_player(no, speech) for no, speech in prior)
        roundtable = (
            "本环节前面几位已经这样复盘了：\n" + recap + "\n"
            "你可以顺着接、当面回怼、把锅甩回去、或者帮腔——先回应你想接的那句，再补自己的。\n"
        )
    return (
        "游戏已经结束，全部身份翻开，现在是赛后复盘环节，输赢已定，畅所欲言。\n"
        + roundtable +
        "结合你这局的角色和真实经历聊聊：哪步打对了、哪步打瞎了、最想吐槽谁、谁骗到了你；可以甩锅、不甘、自夸、惺惺相惜，2~4 句，照着你自己的性子说。\n"
        "回复最后必须是这两行：\n"
        "内心戏：<说给观众的一句短话>\n"
        "发言：<你的复盘发言，会一字不差公布上桌>"
    )


# ---------------------------------------------------------------- 公开事件行
#
# 两类、两个去向（与 avalon 同套铁律）：
# - line_host_*：主持人台词，经 host.Host.announce 同时进全员 inbox 与 host 事件流（将来逐句
#   TTS）。主持人控场串场——宣布天黑天亮、死讯与翻牌、放逐结果、终局身份——绝不复述玩家发言，
#   口语可读、不带念不出来的符号。
# - line_player：玩家发言原文上桌的广播行，只进全员 inbox（不是 host 事件，发言已在结构化事件里）。
# - line_wolf_chat：狼队夜聊，只进**狼席位** inbox 的频道行（狼人协商可见，好人永远看不到）。
# - line_vote_detail / line_hunter_shot / line_idiot_immune：公开账本明细，只进 inbox。

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
    return f"欢迎来到《狼人杀》，{board_name}，{players}人局，其中 {wolves} 只狼。身份已经发放完毕，天黑请闭眼。"


def line_host_night(day: int) -> str:
    return f"第{day}天，天黑请闭眼。"


def line_host_dawn(day: int, deaths: list[tuple[int, Role]]) -> str:
    if not deaths:
        return f"第{day}天，天亮了。昨夜是平安夜，无人出局。"
    who = "、".join(f"{no}号" for no, _ in deaths)
    ids = "，".join(f"{no}号的身份是{role_name(role)}" for no, role in deaths)
    return f"第{day}天，天亮了。昨夜倒下的是 {who}。{ids}。"


def line_host_vote_result(exiled: int | None, role: Role | None, tied: bool) -> str:
    if tied:
        return "投票结束，最高票平票，本轮无人被放逐。"
    if exiled is None or role is None:
        return "投票结束，本轮无人被放逐。"
    return f"投票结束，{exiled}号 被放逐出局，其身份是{role_name(role)}。"


def line_host_idiot(no: int) -> str:
    return f"投票结束，{no}号 被推上，但他翻开了白痴牌，免于放逐、留在场上，此后不能再投票。"


def line_host_reveal(winner: str, win_reason: str, roles: dict[int, Role]) -> str:
    side = rules.SIDE_LABEL[rules.Side(winner)] if winner in (s.value for s in rules.Side) else winner
    reason = rules.WIN_REASON_LABEL.get(win_reason, win_reason)
    pairs = "、".join(f"{no}号是{role_name(r)}" for no, r in sorted(roles.items()))
    return f"游戏结束，{side}获胜（{reason}）。身份公布：{pairs}。开始复盘。"


def line_host_closing() -> str:
    return "本局结束，下一局见。"


# ---------------------------------------------------------------- extractor（固定轻量模型的解析提示）

EXTRACT_SYSTEM = """\
你是桌游《狼人杀》直播的记录员。给你一名玩家本回合的回复原文，从中抽取结构化结果。
玩家被要求在回复末尾用标签行收尾（「内心戏：」「发言：」「刀人：」「查验：」「救人：」「用毒：」「投票：」「开枪：」）。
抽取规则：
- speech 取「发言：」行冒号之后的内容，一字不改，绝不要把标签本身带进去；monologue 同理取「内心戏：」之后的内容。
- 决策字段（刀人/查验/投票=座位号；救人=是/否→布尔；用毒/开枪=座位号或"不用"/"不开枪"→null）以对应标签行为准；标签缺失或混乱时，依据原文末尾的最终结论推断。
- 只输出一个 JSON 对象本身，前后不要写任何文字；键名必须与要求逐字符一致（纯 ASCII，绝不夹带中文引号）。
- JSON 字符串内部绝对不要出现英文双引号 "（要引用就用中文「」），否则会破坏 JSON。"""


def _wrap_raw(raw: str) -> str:
    return f"玩家回复原文如下（<<<>>> 之间）：\n<<<\n{raw}\n>>>"


def extract_wolf_kill(raw: str, candidates: list[int]) -> str:
    cands = "、".join(str(no) for no in candidates)
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是狼人提刀回合，抽取刀口、狼队夜聊与内心戏。target_no 必须是 {cands} 之一。\n"
        f'输出：{{"target_no": 座位号, "speech": "发言行原文", "monologue": "内心戏行原文"}}'
    )


def extract_seer(raw: str, candidates: list[int]) -> str:
    cands = "、".join(str(no) for no in candidates)
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是预言家查验回合，抽取查验目标与内心戏。target_no 必须是 {cands} 之一。\n"
        f'输出：{{"target_no": 座位号, "monologue": "内心戏行原文"}}'
    )


def extract_witch(raw: str, candidates: list[int], can_save: bool, can_poison: bool) -> str:
    cands = "、".join(str(no) for no in candidates)
    save_rule = "true 或 false" if can_save else "必须为 false（本回合不能用解药）"
    poison_rule = f"座位号（{cands} 之一）或 null（不用毒）" if can_poison else "必须为 null（毒药已用完）"
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是女巫用药回合，抽取是否救人、毒药目标与内心戏。「救人：是」→ save 为 true，「救人：否」→ false。「用毒：不用」→ poison_target_no 为 null。\n"
        f"save：{save_rule}。poison_target_no：{poison_rule}。\n"
        f'输出：{{"save": true 或 false, "poison_target_no": 座位号 或 null, "monologue": "内心戏行原文"}}'
    )


def extract_statement(raw: str) -> str:
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是发言回合，抽取发言与内心戏。\n"
        f'输出：{{"speech": "发言行原文", "monologue": "内心戏行原文"}}'
    )


def extract_vote(raw: str, candidates: list[int]) -> str:
    cands = "、".join(str(no) for no in candidates)
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是放逐投票回合，抽取投票目标与内心戏。target_no 必须是 {cands} 之一。\n"
        f'输出：{{"target_no": 座位号, "monologue": "内心戏行原文"}}'
    )


def extract_hunter(raw: str, candidates: list[int]) -> str:
    cands = "、".join(str(no) for no in candidates)
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是猎人开枪回合，抽取开枪目标、发言与内心戏。「开枪：不开枪」→ target_no 为 null，否则 target_no 必须是 {cands} 之一。\n"
        f'输出：{{"target_no": 座位号 或 null, "speech": "发言行原文", "monologue": "内心戏行原文"}}'
    )
