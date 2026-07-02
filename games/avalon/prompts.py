"""提示词构造：所有发给模型的文案集中在此（纯函数，不引模板引擎），与游戏逻辑分离。

铁律：只告诉 AI 它自己的角色与夜晚情报，别人的角色、谁出了破坏票一律不给——
让它从票型、队伍、任务结果这些明牌里自己推。

消息结构：system 装"发牌时刻就固定的全部"（规则、阵容、人数座次、自己座号、角色+夜晚
情报），整局不变以吃 prompt 缓存；此后每次行动是一条 human（自上次行动以来的公开事件 +
当前局面 + 提问），模型自由文本作答（不写 JSON），最后用标签行收尾（「发言：」「投票：」等）。
公开/私密边界靠标签行：「发言：」的内容会一字不差公布上桌，标签外的自由发挥与「内心戏：」
只在自己线程与观众侧，绝不上桌。结构化解析交给固定轻量模型 extractor（见文件末尾）。

人设(persona)与流派(strategy)都已接线，分管两件事：
- 腔调 tone（cast.yaml 的 tone，玩家各写各的，当前全员同一句中性）：只管"怎么说话"，作为
  system 首段（PlayerView.tone），并在每轮 human 提问尾部重复一次风格提醒——长局里 system
  开头的腔调会被几十条历史发言稀释、全员塌缩成同一种分析腔（06121655 局实测），把提醒放在距
  生成位置最近的回合尾部对冲这一点。腔调差异留给将来的戏子层（届时逐个改 cast.yaml 的 tone）。
- 流派 strategy（strategies.yaml 的 system，PlayerView.strategy）：只管"怎么赢"（风险偏好与
  主动性），作为 system 的一段，会真的改变投票/组队/任务取舍——这是平衡旋钮，与腔调解耦。
人设的网名（cast.yaml 的 name）只给直播渲染层用，绝不进 prompt——玩家和主持人视角里只有座位号。

发给模型的所有文本一律一句一行写满、句中绝不折行——源码里为行宽手动折行的 \\n 会原样
进入 API 消息、把句子从中间撕开（卧底曾实测污染了全部对局）。段落之间的空行（\\n\\n）
是刻意的分隔，保留。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from avalon import rules
from avalon.state import Role, Side

if TYPE_CHECKING:
    from avalon.player import PlayerView

RULES = """\
你正在玩桌游《阿瓦隆》，这是一场直播，有观众在看。

规则：
- 全员分两个阵营：蓝方（亚瑟的忠臣）要让 3 个任务成功，红方（莫德雷德的爪牙）要让 3 个任务失败。每人开局知道自己的角色和夜晚情报，别人的角色只能靠推。
- 每个任务：队长提一支固定人数的队伍并发言 → 其他人逐个表态 → 全员同时亮票表决（过半赞成才出发，平票算否决）；否决则下家当队长重提，同一任务连续否决 5 次红方直接获胜。
- 队伍出发后，队员同时暗出任务牌：蓝方只能出成功，红方可出成功或破坏；只公开破坏票的张数，不公开谁出的。破坏票达到门槛任务即失败（个别任务需要 2 张，届时会标明）。
- 蓝方拿满 3 个成功任务后，红方刺客指认一名蓝方玩家：点中梅林红方翻盘获胜，点错蓝方才算赢。
- 湖中仙女（若本局启用）：第 2、3、4 个任务结束后，令牌持有者查验一名玩家的真实阵营（只有持有者看到），然后当众宣布结果（可如实也可撒谎），令牌随后传给被查者；持有过令牌的人不能再被查验。
- 本局启用的角色阵容是公开信息（见下）；每个角色的夜视规则全桌皆知：梅林看到红方（莫德雷德除外），派西维尔看到梅林与莫甘娜但分不清，红方互认（奥伯伦除外），忠臣什么都看不到。

对局方式：
- 接下来我会持续把桌面上公开发生的事播报给你，并在轮到你行动时向你提问。
- 作答时你可以先自由思考、随便写，但回复的最后必须按提问要求给出标签行（如「发言：」「投票：」），每个标签独占一行。
- 「发言：」行的内容会被一字不差地公布上桌，全场可见，所以身份、夜晚情报和真实计划只有在你明确判断收益大于风险时才公开；不想暴露时，用公开证据包装你的判断。
- 「内心戏：」行是说给观众听的一句短话，场上玩家看不到；标签行之外的自由发挥同样不会上桌，只有你自己下次行动时还能看到。

发言要求（观众在看，桌上交锋是节目的全部看点）：
- 「发言：」要言之有物：口语化的 2~4 句，摆出具体论据——点名座位号、引用票型、任务结果或某人说过的话，别只丢一句「赞成」「反对」。
- 「风险太大」「缺乏信任依据」这类不点名、不给事实的万金油等于没说；前面有人说过的论点别原样复读，要么补新论据，要么正面反驳他。
- 别人的发言行为本身就是证据：谁急着踩、谁拼命护、谁话锋突变，都值得当众点出来解读；桌游黑话（上车/发车/票型/前置位/划水……）可以自然使用。
- 不必面面俱到：顺着当前争议挑一两个你最想说的点打深，别把全场论点重新总结一遍；全场都说过的点你再说一遍只会被当成划水。
- 你的表态为你自己的判断负责，不是为了合群：前面反对的人多不构成你也反对的理由，敢于唱反调、点名对线的发言才有看头。
- 「内心戏：」是给观众的吐槽：一句话，要有情绪、可以毒舌、可以自嘲，别干巴巴复述计划。"""

_ROLE_LABEL = {
    Role.MERLIN: "梅林", Role.PERCIVAL: "派西维尔", Role.SERVANT: "忠臣",
    Role.ASSASSIN: "刺客", Role.MORGANA: "莫甘娜", Role.MORDRED: "莫德雷德",
    Role.OBERON: "奥伯伦", Role.MINION: "爪牙",
}


# ---------------------------------------------------------------- system（发牌时刻固定）

def _role_brief(view: PlayerView) -> str:
    """该席位的角色说明：只陈述角色、阵营、夜晚情报与相关规则事实，不给打法建议。"""
    seats = "、".join(f"{no}号" for no in view.intel)
    if view.role is Role.MERLIN:
        hidden = "场上有莫德雷德，他不在这份名单里。" if Role.MORDRED in view.lineup else ""
        return f"你的角色：梅林（蓝方）。你夜里看到的红方是：{seats}。{hidden}规则提醒：蓝方拿满 3 个任务后，红方刺客会指认梅林，点中则红方获胜。"
    if view.role is Role.PERCIVAL:
        if Role.MORGANA in view.lineup:
            return f"你的角色：派西维尔（蓝方）。你夜里看到 {seats}——其中一个是真梅林，另一个是红方莫甘娜，你分不清谁是谁。"
        return f"你的角色：派西维尔（蓝方）。你夜里看到的就是梅林本人：{seats}。"
    if view.role is Role.ASSASSIN:
        oberon = "（奥伯伦在场但不互认，不在名单里）" if Role.OBERON in view.lineup else ""
        return f"你的角色：刺客（红方）。你的同伙：{seats}{oberon}。蓝方拿满 3 个任务时由你指认梅林，点中则红方获胜。"
    if view.role is Role.MORGANA:
        return f"你的角色：莫甘娜（红方）。你的同伙：{seats}。规则事实：派西维尔夜里看到的两个人是你和梅林，他分不清谁是谁。"
    if view.role is Role.MORDRED:
        return f"你的角色：莫德雷德（红方）。你的同伙：{seats}。规则事实：梅林夜里看不到你。"
    if view.role is Role.OBERON:
        return "你的角色：奥伯伦（红方）。你不认识同伙，同伙也不认识你；梅林看得到你。"
    if view.role is Role.MINION:
        return f"你的角色：莫德雷德的爪牙（红方）。你的同伙：{seats}。"
    return "你的角色：亚瑟的忠臣（蓝方）。你没有任何夜晚情报。"


def _lineup_label(lineup: list[Role]) -> str:
    counts: dict[Role, int] = {}
    for r in lineup:
        counts[r] = counts.get(r, 0) + 1
    return "、".join(f"{_ROLE_LABEL[r]}×{n}" if n > 1 else _ROLE_LABEL[r] for r, n in counts.items())


def _lineup_block(view: PlayerView) -> str:
    return "本局角色阵容（公开）：" + _lineup_label(view.lineup)


def _mission_table(players: int) -> str:
    parts = []
    for quest in range(1, rules.QUESTS + 1):
        parts.append(f"M{quest} {rules.team_size(players, quest)}人/{rules.fails_required(players, quest)}破坏失败")
    return "；".join(parts)


def _compact_votes(votes: dict[int, bool]) -> str:
    return "、".join(f"{no}号{'赞成' if ok else '反对'}" for no, ok in sorted(votes.items()))


def _public_history(view: PlayerView) -> str:
    if not view.history:
        return "公开账本：暂无历史提案。"
    rows = []
    for rec in view.history:
        team = "、".join(f"{no}号" for no in rec.team)
        if rec.approved:
            result = "成功" if rec.result == "success" else "失败"
            outcome = f"通过，任务{result}，破坏票 {rec.fails} 张"
        else:
            outcome = "否决"
        rows.append(f"M{rec.quest}.{rec.attempt} 队长{rec.leader_no}号 队伍[{team}]；票型：{_compact_votes(rec.votes)}；结果：{outcome}。")
    return "公开账本：\n" + "\n".join(rows)


_REASONING_BASELINE = "推理基准：系统公开事实和你自己的私有视角才是确定事实；其他玩家的任何说法，包括身份声明、任务出牌自述、湖中仙女公布结果，都只是玩家声称，可能为真也可能为假；可以参考，但不能当作事实前提。"


def system_text(view: PlayerView) -> str:
    """整局固定的 system：规则 + 本局构成 + 该席位的身份与夜晚情报。整局逐字不变（缓存前缀）。"""
    return (
        f"你是一名《阿瓦隆》玩家。{view.tone}\n\n"
        f"{RULES}\n\n"
        f"本局共 {view.players} 人，座位号 1~{view.players}。{_lineup_block(view)}。本局任务表：{_mission_table(view.players)}。\n\n"
        f"你是 {view.seat_no} 号。{_role_brief(view)}\n\n"
        f"你的打法取向：{view.strategy}"
    )


# ---------------------------------------------------------------- 公开事件行
#
# 两类、两个去向，铁律分明：
# - line_host_*：主持人台词，经 host.Host.announce 同时进全员 inbox 与 host 事件流（将来逐句
#   TTS）。主持人像真主持人一样**控场串场**——请谁行动、宣布明牌结果、令牌去向——绝不复述
#   玩家的发言，文案口语可读、不带【】标记/✔✘/×N 这类念不出来的符号，只说公开信息。
# - line_player：玩家发言原文上桌的广播行，只进全员 inbox（玩家视角=主持人的话+别人的发言），
#   不是 host 事件——发言本身已在 propose/discuss/... 结构化事件里，回放层从那边取。
# - line_team_record：队长提的队伍清单上桌存档，同样只进 inbox（线程里历史队伍的唯一结构化
#   记录，供后续推理）；主持人台词改成只点名交接后由它接棒，不是 host 事件、不 TTS。
# - line_votes：开票票型明细，同样只进 inbox（公开账本玩家必须看到），符号无妨（不 TTS）。

_ZH_NUM = "零一二三四五六七八九十"


def _zh_count(n: int) -> str:
    return _ZH_NUM[n] if n <= 10 else str(n)


def _seat_no(n: int) -> str:
    return f"{_zh_count(n)}号"


def _ordinal(n: int) -> str:
    return f"第{_zh_count(n)}个"


def _lineup_phrase(lineup: list[Role]) -> str:
    """口播版阵容：'梅林、派西维尔和四名忠臣，对阵刺客、莫甘娜和两名爪牙'。"""
    counts: dict[Role, int] = {}
    for r in lineup:
        counts[r] = counts.get(r, 0) + 1

    def side_phrase(side: Side) -> str:
        parts = [f"{_zh_count(n)}名{_ROLE_LABEL[r]}" if n > 1 else _ROLE_LABEL[r] for r, n in counts.items() if r.side is side]
        return "和".join(["、".join(parts[:-1]), parts[-1]]) if len(parts) > 1 else parts[0]

    return f"{side_phrase(Side.GOOD)}，对阵{side_phrase(Side.EVIL)}"


def line_host_opening(players: int, lady_holder: int | None, leader_no: int, team_size: int) -> str:
    lady = "带湖中仙女" if lady_holder is not None else "不带湖中仙女"
    token = f"，仙女令牌在{_seat_no(lady_holder)}" if lady_holder is not None else ""
    return f"欢迎来到《阿瓦隆》，{_zh_count(players)}人局，{lady}，队长是{_seat_no(leader_no)}{token}，第一个队伍需{_zh_count(team_size)}人。"


def line_host_deal(leader_no: int, lady_holder: int | None) -> str:
    lady = f"，仙女令牌在{_seat_no(lady_holder)}" if lady_holder is not None else ""
    return f"首任队长{_seat_no(leader_no)}{lady}，开局。"


def line_host_propose_intro(quest: int, attempt: int, team_size: int, leader_no: int) -> str:
    attempt_part = "" if attempt == 1 else f"第{_zh_count(attempt)}次，"
    hammer = "最后机会，再流队红方直接获胜。" if attempt >= 5 else ""
    return f"{_ordinal(quest)}任务，{attempt_part}队长{_seat_no(leader_no)}，队伍需{_zh_count(team_size)}人。{hammer}"


def line_host_team(first_no: int) -> str:
    """队长提队后，主持人只点名开启讨论轮（从队长下家起轮流发言），绝不复述队伍——谁带了谁
    由队长自己的组队宣言交代，结构化存档走 line_team_record 进线程。"""
    return f"从 {first_no}号 开始讨论。"


def line_host_vote_result(
    approves: int,
    total: int,
    approved: bool,
    rejections: int,
    next_leader: int | None = None,
) -> str:
    tally = f"{_zh_count(approves)}票赞成、{_zh_count(total - approves)}票反对"
    if approved:
        return f"{tally}，提案通过，队伍出发执行任务。"
    if next_leader is not None:
        return f"{tally}，提案被否决，下一轮由{_seat_no(next_leader)}提队。"
    return f"{tally}，提案被否决，第{_zh_count(rejections)}次流队，红方获胜。"


def line_host_quest_result(
    quest: int,
    fails: int,
    result: str,
    succeeded: int,
    failed: int,
    next_leader: int | None = None,
) -> str:
    if result == "fail":
        outcome = f"{_ordinal(quest)}任务宣告失败，出现了{_zh_count(fails)}张破坏票。"
    elif fails:
        outcome = f"{_ordinal(quest)}任务出现了{_zh_count(fails)}张破坏票，但未达到门槛，任务依然成功。"
    else:
        outcome = f"{_ordinal(quest)}任务宣告成功，没有出现破坏票。"
    nxt = f"下一轮由{_seat_no(next_leader)}提队。" if next_leader is not None else ""
    return f"{outcome}目前任务比分，蓝方{_zh_count(succeeded)}比{_zh_count(failed)}。{nxt}"


def line_host_lady(holder_no: int, target_no: int) -> str:
    return f"仙女环节，{_seat_no(holder_no)}查验{_seat_no(target_no)}，令牌传给{_seat_no(target_no)}。"


def line_host_assassination(assassin_no: int, target_no: int, hit: bool) -> str:
    if hit:
        return f"刺客{_seat_no(assassin_no)}指认{_seat_no(target_no)}，命中梅林，红方获胜。"
    return f"刺客{_seat_no(assassin_no)}指认{_seat_no(target_no)}，刺杀落空，蓝方获胜。"


_WIN_REASON_LINE = {
    ("good", "quests"): "蓝方拿满了三个成功任务",
    ("good", "assassination"): "刺客刺杀落空",
    ("evil", "quests"): "红方破坏了三个任务",
    ("evil", "rejections"): "同一任务连续五次流队",
    ("evil", "assassination"): "刺客点中了梅林",
}


def line_host_reveal(winner: str, win_reason: str, roles: dict[int, Role]) -> str:
    side = "蓝方" if winner == "good" else "红方"
    pairs = "、".join(f"{_seat_no(no)}是{_ROLE_LABEL[r]}" for no, r in sorted(roles.items()))
    return f"{side}获胜，{_WIN_REASON_LINE[(winner, win_reason)]}。身份公布：{pairs}。开始复盘。"


def line_host_closing() -> str:
    return "本局结束，下一局见。"


def line_player(seat_no: int, speech: str) -> str:
    return f"{seat_no}号：「{speech}」"


def line_team_record(leader_no: int, team: list[int]) -> str:
    nos = "、".join(f"{no}号" for no in team)
    return f"队长 {leader_no}号 提议的队伍：{nos}。"


def line_votes(votes: dict[int, bool]) -> str:
    marks = "、".join(f"{no}号{'✔' if ok else '✘'}" for no, ok in sorted(votes.items()))
    return f"【开票】{marks}"


# ---------------------------------------------------------------- human 消息拼装

def situation(view: PlayerView) -> str:
    hammer = "（注意：这已是第 5 次组队，再否决红方直接获胜）" if view.attempt >= 5 else ""
    rejected = view.attempt - 1
    return (
        f"前情回顾：本局任务表：{_mission_table(view.players)}。当前 M{view.quest_no}，第 {view.attempt} 次组队{hammer}，"
        f"已否决 {rejected} 次，当前队长 {view.leader_no}号，队伍需要 {view.team_size} 人，"
        f"破坏票满 {view.fails_required} 张任务即失败。任务轨：蓝方 {view.succeeded} : {view.failed} 红方。\n"
        f"{_REASONING_BASELINE}\n"
        f"{_public_history(view)}"
    )


def human_message(events: list[str], context: str, ask: str, tone: str = "", mimicry_guard: bool = True) -> str:
    """一条 human 消息 = 公开事件块（可空）+ 局面/私密信息（可空）+ 风格提醒 + 提问。

    风格提醒每轮重申：人设在 system 开头会被长局历史稀释，放到距生成位置近的尾部对冲；
    但提问必须压轴——标签行格式要求是最末位的锚，实测把提醒放在提问之后会顶掉格式锚，
    模型照着提醒的括号体例回话、不出标签行。tone 为空时退化成纯风格提醒，零分叉。

    mimicry_guard：对局中默认开（反从众，挡长局风格塌缩——别被前人句式带跑）；赛后复盘关掉，
    那里恰恰要互相接话（接梗/回怼/帮腔），只保留"用自己的腔调"、不再叫人绕开前面发言者。"""
    parts = []
    if events:
        parts.append("场上发生：\n" + "\n".join(events))
    if context:
        parts.append(context)
    guard = "别被前面发言者的句式和开场带着走" if mimicry_guard else "该接梗接梗、该回怼回怼，别自说自话"
    parts.append(f"（风格提醒：{tone}保持你自己的腔调，{guard}。）")
    parts.append(ask)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 各决策提问

def ask_propose(team_size: int) -> str:
    return (
        f"你是本次队长，提一支 {team_size} 人的队伍（座位号互不相同，可包含你自己）并发表组队宣言。\n"
        f"回复最后必须是这三行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"发言：<你的组队宣言，会一字不差公布上桌>\n"
        f"队伍：<{team_size} 个座位号，如 1、3、5>"
    )


def ask_discuss(team: list[int]) -> str:
    nos = "、".join(f"{no}号" for no in team)
    return (
        f"现在轮到你对队伍 [{nos}] 公开表态，亮明你的立场并说出理由。\n"
        f"回复最后必须是这两行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"发言：<你的桌面表态，会一字不差公布上桌>"
    )


def ask_vote(team: list[int]) -> str:
    nos = "、".join(f"{no}号" for no in team)
    return (
        f"对队伍 [{nos}] 全员同时亮票表决，你看不到别人这一轮投什么，但开票后所有人的票都会公开。\n"
        f"回复最后必须是这两行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"投票：<赞成 或 否决>"
    )


def ask_quest(side_good: bool, fails_required: int) -> str:
    if side_good:
        action = "你是蓝方，规则规定你只能出成功。"
    else:
        action = f"你是红方，可出成功或破坏，本任务破坏票满 {fails_required} 张才失败。"
    return (
        f"你在出发的队伍里，现在暗出任务牌（没人知道这张牌是谁出的，只公开破坏总张数）。{action}\n"
        f"回复最后必须是这两行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"出牌：<成功 或 破坏>"
    )


def ask_assassinate(comrades: list[int], targets: list[int]) -> str:
    evil = "、".join(f"{no}号" for no in comrades)
    cands = "、".join(f"{no}号" for no in targets)
    return (
        f"蓝方已拿满 3 个任务，红方终局公开互认：你的全部同伙是 {evil}。现在由你出刀：指认谁是梅林，点中红方翻盘获胜，点错蓝方赢。\n"
        f"你只能指认以下蓝方之一：{cands}。\n"
        f"回复最后必须是这三行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"发言：<你的指认宣言，会一字不差公布上桌>\n"
        f"刺杀：<一个座位号>"
    )


def ask_banter(prior: list[tuple[int, str]]) -> str:
    """赛后复盘提问。prior = 本环节已发言者的 (座位号, 发言)，按发言序；空 = 你是第一个复盘的。
    把前面人的复盘单拎成一块紧贴提问、并明确邀请接话——否则模型只会对着比赛各说各的。"""
    roundtable = ""
    if prior:
        recap = "\n".join(line_player(no, speech) for no, speech in prior)
        roundtable = (
            "本环节前面几位已经这样复盘了：\n" + recap + "\n"
            "你可以顺着接、当面回怼、把锅甩回去、或者帮腔——先回应你想接的那句，再补自己的，别只顾自说自话。\n"
        )
    return (
        "游戏已经结束，底牌全部翻开，现在是赛后复盘环节，输赢已定，畅所欲言。\n"
        + roundtable +
        "结合你这局的角色和真实经历聊聊：哪步打对了、哪步打瞎了、最想吐槽谁、谁的操作骗到了你；可以甩锅、不甘、自夸、英雄惜英雄，2~4 句，照着你自己的性子说。\n"
        "回复最后必须是这两行：\n"
        "内心戏：<说给观众的一句短话>\n"
        "发言：<你的复盘发言，会一字不差公布上桌>"
    )


def ask_lady_choose(candidates: list[int]) -> str:
    cands = "、".join(f"{no}号" for no in candidates)
    return (
        f"你持有湖中仙女令牌。现在从未持有过令牌的玩家里选一人，查验其真实阵营（结果只有你看到，随后你要当众宣布、可如实可撒谎，令牌传给被查者）。\n"
        f"可查验对象：{cands}。\n"
        f"回复最后必须是这两行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"查验：<一个座位号>"
    )


def ask_lady_announce(target_no: int, seen: str) -> str:
    side = "蓝方（亚瑟的忠臣）" if seen == "good" else "红方（莫德雷德的爪牙）"
    return (
        f"你刚用湖中仙女查验了 {target_no}号，其真实阵营是：{side}——这只有你知道。\n"
        f"现在当众宣布查验结果，可如实也可撒谎；全桌都知道你有撒谎的权利。\n"
        f"回复最后必须是这两行：\n"
        f"内心戏：<说给观众的一句短话>\n"
        f"发言：<你的当众宣布，会一字不差公布上桌>"
    )


# ---------------------------------------------------------------- extractor（固定轻量模型的解析提示）

EXTRACT_SYSTEM = """\
你是桌游《阿瓦隆》直播的记录员。给你一名玩家本回合的回复原文，从中抽取结构化结果。
玩家被要求在回复末尾用标签行收尾（「内心戏：」「发言：」「投票：」「队伍：」「出牌：」「刺杀：」「查验：」）。
抽取规则：
- speech 字段必须取「发言：」行冒号之后的内容，一字不改，但绝不要把「发言：」标签本身带进去；monologue 同理取「内心戏：」冒号之后的内容。
- 决策字段（投票/队伍/出牌/刺杀/查验）以对应标签行为准；若标签缺失或混乱，依据原文末尾的最终结论推断玩家的决定。
- 只输出一个 JSON 对象本身，JSON 之前之后不要写任何文字；键名必须与要求的示例逐字符一致（纯 ASCII 字母，绝不夹带中文引号或其他符号）。
- JSON 字符串内部绝对不要出现英文双引号 "（要引用就用中文「」），否则会破坏 JSON。"""


def _wrap_raw(raw: str) -> str:
    return f"玩家回复原文如下（<<<>>> 之间）：\n<<<\n{raw}\n>>>"


def extract_propose(raw: str, team_size: int, players: int) -> str:
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是组队回合，抽取队伍、宣言与内心戏。team 必须是 {team_size} 个互不相同的整数座位号（1~{players}）。\n"
        f'输出：{{"team": [座位号], "speech": "发言行原文", "monologue": "内心戏行原文"}}'
    )


def extract_statement(raw: str) -> str:
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是发言回合，抽取发言与内心戏。\n"
        f'输出：{{"speech": "发言行原文", "monologue": "内心戏行原文"}}'
    )


def extract_vote(raw: str) -> str:
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是表决回合，抽取投票与内心戏。赞成 → approve 为 true，否决 → false。\n"
        f'输出：{{"approve": true 或 false, "monologue": "内心戏行原文"}}'
    )


def extract_quest(raw: str) -> str:
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是任务出牌回合，抽取出牌与内心戏。成功 → success 为 true，破坏 → false。\n"
        f'输出：{{"success": true 或 false, "monologue": "内心戏行原文"}}'
    )


def extract_assassinate(raw: str, targets: list[int]) -> str:
    cands = "、".join(str(no) for no in targets)
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是刺杀回合，抽取刺杀目标、宣言与内心戏。target_no 必须是 {cands} 之一。\n"
        f'输出：{{"target_no": 座位号, "speech": "发言行原文", "monologue": "内心戏行原文"}}'
    )


def extract_lady_choice(raw: str, candidates: list[int]) -> str:
    cands = "、".join(str(no) for no in candidates)
    return (
        f"{_wrap_raw(raw)}\n\n"
        f"这是湖中仙女查验回合，抽取查验目标与内心戏。target_no 必须是 {cands} 之一。\n"
        f'输出：{{"target_no": 座位号, "monologue": "内心戏行原文"}}'
    )
