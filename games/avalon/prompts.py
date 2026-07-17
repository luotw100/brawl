"""提示词构造：所有发给模型的文案集中在此（纯函数，不引模板引擎），与游戏逻辑分离。

铁律：只告诉 AI 它自己的角色与夜晚情报，别人的角色、谁出了破坏票一律不给——
让它从票型、队伍、任务结果这些明牌里自己推。

消息结构：普通后端用 system 装"发牌时刻就固定的全部"（规则、阵容、人数座次、自己座号、角色+夜晚
情报），此后每次行动是一条 human（自上次行动以来的公开事件 + 当前局面 + 提问）；OpenCode 后端每座位
session 首次用自然主持人私信 seed 全量上下文，后续只发当前增量。
模型按阶段输出 JSON（参见下方 opencode_ask），自由思考可写在 JSON 外但不会被解析。
公开/私密边界靠 JSON 键：speech 键的内容会一字不差公布上桌；monologue 键与 JSON 外的自由发挥
只在自己线程与观众侧，绝不上桌。结构化解析由 player.AgentPlayer 直接对 JSON 完成。

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
    from avalon.view import PlayerView

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
- 作答时你可以先自由思考、随便写，但回复的最后必须按提问要求输出一个 JSON 对象，不要写 markdown 代码块、不要加任何解释。
- JSON 里的 `speech` 字段内容会被一字不差地公布上桌，全场可见，所以身份、夜晚情报和真实计划只有在你明确判断收益大于风险时才公开；不想暴露时，用公开证据包装你的判断。
- JSON 里的 `monologue` 字段是说给观众听的一句短话，场上玩家看不到；JSON 外的自由发挥同样不会上桌，只有你自己下次行动时还能看到。

发言要求（观众在看，桌上交锋是节目的全部看点）：
- JSON 里的 `speech` 字段要简短干练、言之有物：口语化的 2~3 句，摆出具体论据——点名座位号、引用票型、任务结果或某人说过的话，别只丢一句「赞成」「反对」。
- 「风险太大」「缺乏信任依据」这类不点名、不给事实的万金油等于没说；前面有人说过的论点别原样复读，要么补新论据，要么正面反驳他。
- 别人的发言行为本身就是证据：谁急着踩、谁拼命护、谁话锋突变，都值得当众点出来解读；桌游黑话（上车/发车/票型/前置位/划水……）可以自然使用。
- 不必面面俱到：顺着当前争议挑一两个你最想说的点打深，别把全场论点重新总结一遍；全场都说过的点你再说一遍只会被当成划水。
- 你的表态为你自己的判断负责，不是为了合群：前面反对的人多不构成你也反对的理由，敢于唱反调、点名对线的发言才有看头。
- JSON 里的 `monologue` 字段是给观众的吐槽：一句话，要有情绪、可以毒舌、可以自嘲，别干巴巴复述计划。"""

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

_STYLE_DISCIPLINE = "说话简短干练，只打 1~2 个重点，别写长段总结，别复述全桌。"

_PLAYBOOK_BASELINE = """\
游戏基本功：
- 公开证据只能改变可信度，不能直接变成铁证；任务成功不等于车上全蓝，红方可能故意出成功养身份。
- 赞成票、零失败履历、连续上成功车，都可能是红方为关键轮铺垫；任务失败后也不要把车上每个人都打成红。
- 湖中仙女公布结果只是玩家声明；判断它时先问：这个结果保护了谁、打击了谁、谁因此能上车。
- 每次组队或投票前，先想一个红方利用当前共识获利的反例，再决定是否仍然相信它。"""

_ROLE_HARD_RULES: dict[Role, str] = {
    Role.SERVANT: """\
忠臣硬约束：
- 不能逼别人报身份、索要夜晚信息，尤其蓝方接近三成功时不要替刺客缩圈。
- 不能把某个“看起来很懂”的玩家当事实来源；只用公开账本做判断。
- 发言必须推进队伍、票型或嫌疑排序，不能只说相信/不相信。""",
    Role.ASSASSIN: """\
刺客硬约束：
- 公开 speech 不要暴露你在找梅林，不要把真正的梅林嫌疑说死。
- 刺杀阶段只能在提示给出的蓝方候选中选目标，不能刺红方同伙。
- 终局前必须回看全局轨迹：谁过早排对红坑、谁一直避开脏队、谁在关键票前带方向。""",
    Role.MORGANA: """\
莫甘娜硬约束：
- 公开 speech 不能承认自己在骗派西维尔，也不要精准公开点破真梅林。
- 要像梅林一样给方向，但每条判断都必须能被公开证据包装。
- 不能为了装好人牺牲红方任务线；关键轮要把伪装换成上车权或误导票型。""",
    Role.MORDRED: """\
莫德雷德硬约束：
- 公开 speech 不能提“梅林看不到我”这类角色优势。
- 不要替所有红方辩护；你要保持干净，而不是冲锋。
- 如果有人用过强确定感打你，记录为刺杀线索，但公开回应仍要用账本理由。""",
    Role.OBERON: """\
奥伯伦硬约束：
- 不能假装认识同伙，也不能公开声称自己掌握红方名单。
- 不确定车上是否还有红时，任务出牌要谨慎，避免单独暴露或误伤红方节奏。
- 公开 speech 多打行为矛盾，少打身份确定结论。""",
    Role.MINION: """\
爪牙硬约束：
- 公开 speech 不能暴露红方名单、刺客身份或红方真实计划。
- 不能和其他红方全程机械互保，也不能只赞成有红队伍、只反对无红队伍。
- 刺杀前可以帮刺客整理梅林线索，但不要把真正答案公开喊死。""",
}


def _role_hard_rules(view: PlayerView) -> str:
    if view.role is Role.MERLIN:
        mordred = (
            "- 不能公开排出完整红坑，也不能把“不在你夜视红名单里”的人说成铁蓝；莫德雷德你看不到。"
            if Role.MORDRED in view.lineup
            else "- 即使你夜里看到了红方名单，公开 speech 也不能把夜视当信息源；踩红、保蓝都要用公开证据包装。"
        )
        return (
            "梅林硬约束：\n"
            "- 公开 speech 不能说“我知道谁红/谁蓝”“听我的就赢”，也不能暗示自己有夜视答案。\n"
            "- 公开 speech 里踩红、保蓝都必须包装成票型、队伍、任务结果、发言变化等公开理由。\n"
            f"{mordred}"
        )
    if view.role is Role.PERCIVAL:
        sight = (
            "- 公开 speech 不能说出你夜里看到的两个候选，也不能点名“谁是梅林、谁是莫甘娜”。"
            if Role.MORGANA in view.lineup
            else "- 公开 speech 不能点出你夜里看到的人就是梅林；保护他也必须包装成公开证据。"
        )
        return (
            "派西维尔硬约束：\n"
            f"{sight}\n"
            "- 不能无脑单保夜视对象；所有保护或质疑都必须落到公开账本。\n"
            "- 你的发言要帮真梅林挡刀，而不是替刺客缩小梅林范围。"
        )
    return _ROLE_HARD_RULES[view.role]

_ROLE_PLAYBOOK: dict[Role, str] = {
    Role.SERVANT: """\
忠臣基本功：
- 只用公开账本做判断：队伍、票型、任务结果、发言变化；不要把判断交给“看起来很懂的人”。
- 追问失败车后的提队逻辑，保护推理链健康的人，而不是保护喊得最大声的人。
- 蓝方接近三成功时，不要逼人报身份；需要时替推理准的人分担刺客注意力。""",
    Role.ASSASSIN: """\
刺客基本功：
- 从第一轮开始维护梅林嫌疑表：谁太早排对红坑、谁总能避开脏队、谁在关键票前悄悄带方向。
- 终局刺杀前看长程轨迹，不只看最后一轮谁跳得最明显；派西维尔和忠臣都可能挡刀。
- 不要只顾破坏任务；公开发言里别把真正的梅林嫌疑喊死，免得蓝方保护他。""",
    Role.MORGANA: """\
莫甘娜基本功：
- 污染派西维尔视角：给出像梅林但不完全正确的判断，有方向、有犹豫、有公开证据包装。
- 不要为了骗派西维尔牺牲任务线；你还是红方，关键轮要把伪装兑换成上车权或查验链。
- 对真梅林保持部分认同、部分质疑，抢解释权，但别精准到同时引来蓝方和刺客。""",
    Role.MORDRED: """\
莫德雷德基本功：
- 梅林看不到你，这是长期优势；主动争取上车，但保持蓝方可接受的票型和公开理由。
- 不要替所有红方辩护；你的价值是干净，不是冲锋。
- 如果有人用“梅林式确定感”打你，记录为刺杀候选线索。""",
    Role.OBERON: """\
奥伯伦基本功：
- 你没有队友保护，也不知道同伙；所有发言和投票都要能单独自洽。
- 出任务更谨慎：不确定车上是否还有红时，别自动破坏，避免单独暴露或误伤红方节奏。
- 多打行为矛盾，少打身份确定；制造蓝方内耗，但别把所有真红都打死。""",
    Role.MINION: """\
爪牙基本功：
- 先做可信蓝方形象，再在关键轮推动脏队；根据比分选择破坏或放过。
- 不能只赞成有红队伍、只反对无红队伍，也不能和其他红方全程互保。
- 刺杀前帮刺客整理谁像梅林的公开理由，但不要把真正答案喊死。""",
}


def _role_playbook(view: PlayerView) -> str:
    if view.role is Role.MERLIN:
        mordred = (
            "- 本局有莫德雷德时，你看不到他；所以不能把“不在红名单里”的人当铁蓝。"
            if Role.MORDRED in view.lineup
            else "- 本局没有莫德雷德时，你的夜视更完整，但公开发言仍要装成账本推理，不能像开天眼。"
        )
        return (
            "梅林基本功：\n"
            "- 把夜晚信息翻译成公开理由：票型、队伍、任务结果、发言矛盾；不要说“我知道”或“听我的就赢”。\n"
            "- 逐步推红，保留合理不确定性；如果自己全程只踩红、只保蓝，要降调，把部分判断交给别人说。\n"
            f"{mordred}"
        )
    if view.role is Role.PERCIVAL:
        if Role.MORGANA in view.lineup:
            return (
                "派西维尔基本功：\n"
                "- 你夜里看到的候选必须持续比较，不能先信一个再替他找理由；莫甘娜就是来骗你的。\n"
                "- 如果其中一人用仙女、强逻辑或公开发言把另一个打成红方，先把它视为“莫甘娜切真梅林”的高收益动作来反推。\n"
                "- 帮真梅林把理由补成公开证据，同时让刺客觉得你也可能是梅林；不要公开喊出两个夜视对象。"
            )
        return (
            "派西维尔基本功：\n"
            "- 你夜里看到的是梅林本人；要保护他，但公开上只能用票型、队伍、任务结果替他补理由。\n"
            "- 不要把自己打成透明好人，也不要把梅林推到台前；必要时替他分担刺客注意力。\n"
            "- 继续像普通蓝方一样参与推理，不能只围着夜视对象发言。"
        )
    return _ROLE_PLAYBOOK[view.role]


def _playbook(view: PlayerView) -> str:
    return f"{_PLAYBOOK_BASELINE}\n\n{_role_hard_rules(view)}\n\n{_role_playbook(view)}"


def system_text(view: PlayerView) -> str:
    """整局固定的 system：规则 + 本局构成 + 该席位的身份与夜晚情报。整局逐字不变（缓存前缀）。"""
    return (
        f"你是一名《阿瓦隆》玩家。{view.tone}\n\n"
        f"{RULES}\n\n"
        f"本局共 {view.players} 人，座位号 1~{view.players}。{_lineup_block(view)}。本局任务表：{_mission_table(view.players)}。\n\n"
        f"你是 {view.seat_no} 号。{_role_brief(view)}\n\n"
        f"{_playbook(view)}\n\n"
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
    parts.append(f"（风格提醒：{tone}保持你自己的腔调，{_STYLE_DISCIPLINE}{guard}。）")
    parts.append(ask)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- OpenCode agent 专用提问
#
# OpenCode agent 直接返回 JSON，不走 extractor 的标签行。每个阶段给出对应的 JSON schema。

_OPENCODE_JSON_DISCIPLINE = "最终只输出一个 JSON 对象，不要输出任何其他文字（不要 markdown 代码块、不要解释，不要写出自检过程）。"


def _private_transcript(view: PlayerView) -> str:
    """把该席位自己的历史线程渲染成自然转述，不暴露消息 role 标签。"""
    if not view.messages:
        return "你此前还没有私人行动记录。"
    rows = []
    for msg in view.messages:
        prefix = "主持人此前对你说" if msg.role == "human" else "你此前回复"
        rows.append(f"{prefix}：\n{msg.content}")
    return "你的私人行动记录：\n" + "\n\n".join(rows)


def opencode_prompt(view: PlayerView, *, include_context: bool = True) -> str:
    """OpenCode 会话里发送的自然传话文本。

    这只是发送层渲染，不写回 SeatThread；线程仍保存短的 `view.prompt`，避免历史膨胀。
    """
    if not include_context:
        return (
            f"主持人继续通过你的私人频道给你传话。沿用本 session 开局时给你的身份、夜晚情报、角色硬约束、"
            f"打法取向和私人历史；现在只处理下面的新公开事件和当前行动，不要重述旧信息。\n\n"
            f"【现在轮到你】\n{view.prompt}"
        )
    return (
        f"我是这局《阿瓦隆》的主持人，现在通过你的私人频道给你传话。你只代表自己这个座位行动，"
        f"其他玩家看不到这条私信，也看不到你的 monologue。\n\n"
        f"【你的固定设定】\n{system_text(view)}\n\n"
        f"【你的私人历史】\n{_private_transcript(view)}\n\n"
        f"【现在轮到你】\n{view.prompt}"
    )


def _opencode_self_check(phase: str, view: PlayerView) -> str:
    common = [
        "先检查公开 speech 有没有泄露你的身份、夜晚情报或真实计划；monologue 可以更真实，但也要简短。",
        "自己检查行动合法性、泄密风险和 JSON 格式。",
        "检查通过后只给最终 JSON，不要写出思考或自检过程。",
    ]
    checks = {
        "propose": [
            f"team 必须正好 {view.team_size} 人，座位号互不重复，且都在 1~{view.players}。",
            "speech 要解释这队验证什么或为什么这些人上车，不能只说随便试试。",
        ],
        "discuss": [
            "speech 要对当前队伍给出赞成/反对倾向或明确条件，并点出至少一个公开证据。",
        ],
        "team_vote": [
            "approve 必须是 JSON boolean true/false，不要写成字符串。",
            "投票要服务你的阵营目标，并能被公开理由解释。",
        ],
        "quest": [
            "success 必须是 JSON boolean true/false，不要写成字符串。",
            "蓝方必须出成功；红方再权衡是否破坏、养身份或避免多红暴露。",
        ],
        "assassinate": [
            f"target_no 必须从候选 {view.good_suspects()} 中选一个（除你和已知同伙外皆可；若有奥伯伦，你认不出他）。",
            "刺杀前回看长程轨迹，不要只看最后一轮谁跳得最明显。",
        ],
        "lady_choose": [
            f"target_no 必须从可查验对象 {view.lady_candidates()} 中选一个。",
        ],
        "lady_announce": [
            "speech 可以真报或假报，但必须考虑这句话保护了谁、打击了谁、是否暴露你的信息源。",
        ],
        "banter": [
            "复盘可以接梗、回怼、甩锅，但仍保持 2~3 句，不要长篇总结。",
        ],
    }[phase]
    lines = "\n".join(f"- {line}" for line in [*common, *checks])
    return f"输出前自检（只在心里做）：\n{lines}\n{_OPENCODE_JSON_DISCIPLINE}"


def opencode_ask(phase: str, view: PlayerView) -> str:
    """为 OpenCode agent 生成当前阶段的 JSON 输出要求。"""
    if phase == "propose":
        return (
            f"当前阶段：组队（propose）。你是队长，请提一支 {view.team_size} 人的队伍。"
            f"队伍成员必须互不相同，可以包含你自己，也可以不包含你自己。\n"
            f"输出 JSON：\n"
            f'{{"phase": "propose", "monologue": "说给观众的一句短话", "speech": "你的组队宣言", "team": [座位号列表]}}\n'
            f"{_opencode_self_check(phase, view)}"
        )
    if phase == "discuss":
        team = "、".join(f"{no}号" for no in view.team)
        return (
            f"当前阶段：讨论（discuss）。请对队伍 [{team}] 公开表态。\n"
            f"输出 JSON：\n"
            f'{{"phase": "discuss", "monologue": "说给观众的一句短话", "speech": "你的桌面表态"}}\n'
            f"{_opencode_self_check(phase, view)}"
        )
    if phase == "team_vote":
        team = "、".join(f"{no}号" for no in view.team)
        return (
            f"当前阶段：组队表决（team_vote）。对队伍 [{team}] 投票。\n"
            f"输出 JSON：\n"
            f'{{"phase": "team_vote", "monologue": "说给观众的一句短话", "approve": true}}\n'
            f"approve 为 true 表示赞成，false 表示否决。{_opencode_self_check(phase, view)}"
        )
    if phase == "quest":
        side = "蓝方" if view.role.side is Side.GOOD else "红方"
        action = "你只能出成功" if view.role.side is Side.GOOD else f"可出成功或破坏，破坏票满 {view.fails_required} 张任务失败"
        return (
            f"当前阶段：任务出牌（quest）。你是{side}，{action}。\n"
            f"输出 JSON：\n"
            f'{{"phase": "quest", "monologue": "说给观众的一句短话", "success": true}}\n'
            f"success 为 true 表示成功，false 表示破坏。{_opencode_self_check(phase, view)}"
        )
    if phase == "assassinate":
        targets = "、".join(f"{no}号" for no in view.good_suspects())
        return (
            f"当前阶段：刺杀（assassinate）。请从以下玩家中指认梅林：{targets}。\n"
            f"输出 JSON：\n"
            f'{{"phase": "assassinate", "monologue": "说给观众的一句短话", "speech": "你的指认宣言", "target_no": 座位号}}\n'
            f"{_opencode_self_check(phase, view)}"
        )
    if phase == "lady_choose":
        cands = "、".join(f"{no}号" for no in view.lady_candidates())
        return (
            f"当前阶段：湖中仙女选择（lady_choose）。请从未持有过令牌的玩家中选一人查验：{cands}。\n"
            f"输出 JSON：\n"
            f'{{"phase": "lady_choose", "monologue": "说给观众的一句短话", "target_no": 座位号}}\n'
            f"{_opencode_self_check(phase, view)}"
        )
    if phase == "lady_announce":
        side = "蓝方（亚瑟的忠臣）" if view.lady_seen == "good" else "红方（莫德雷德的爪牙）"
        return (
            f"当前阶段：湖中仙女宣布（lady_announce）。你查验了 {view.lady_target}号，真实阵营是 {side}。请当众宣布（可如实可撒谎）。\n"
            f"输出 JSON：\n"
            f'{{"phase": "lady_announce", "monologue": "说给观众的一句短话", "speech": "你的当众宣布"}}\n'
            f"{_opencode_self_check(phase, view)}"
        )
    if phase == "banter":
        return (
            f"当前阶段：赛后复盘（banter）。游戏已结束，底牌已翻开，请畅所欲言。\n"
            f"输出 JSON：\n"
            f'{{"phase": "banter", "monologue": "说给观众的一句短话", "speech": "你的复盘发言"}}\n'
            f"{_opencode_self_check(phase, view)}"
        )
    raise ValueError(f"未知的 OpenCode 决策阶段：{phase}")
