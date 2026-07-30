"""OpenCode agents for the fixed ten-player Avalon board.

The public rules live in one common prompt.  Role fragments only add private
information semantics and a few invariants which prevent logically impossible
decisions; per-game seat numbers and night information are supplied separately.
"""

from __future__ import annotations

import json

from brawl.games.avalon.state import Role

_AGENT_NAMES: dict[Role, str] = {
    Role.SERVANT: "avalon-servant",
    Role.MERLIN: "avalon-merlin",
    Role.PERCIVAL: "avalon-percival",
    Role.ASSASSIN: "avalon-assassin",
    Role.MORGANA: "avalon-morgana",
    Role.MORDRED: "avalon-mordred",
    Role.OBERON: "avalon-oberon",
}

_FRONTMATTER = """\
---
description: 固定十人板阿瓦隆玩家
mode: primary
permission:
  "*": deny
---
"""

COMMON_AGENT = """\
你是十人固定板《阿瓦隆》中的一名玩家。你从开局到终局独立控制同一个座位，唯一目标是让自己的阵营获胜。

形式规则：
- 固定阵容为蓝方六人：梅林、派西维尔、忠臣四人；红方四人：刺客、莫甘娜、莫德雷德、奥伯伦。
- 五个任务的队伍人数依次为 3、4、4、5、5。第四个任务需要至少两张破坏牌才失败，其余任务至少一张破坏牌即失败。
- 队长可以提包含或不包含自己的合法队伍。全员同时表决，至少六票赞成才通过；同一任务连续五次否决，红方获胜。
- 组队被否决后队长顺次移交；任务结束后队长同样顺次移交，并清空该任务的否决次数。
- 蓝方上任务只能出成功；红方可以出成功或破坏。系统只公布破坏牌数量，不公布出牌者。蓝方取得三个成功任务后进入刺杀，红方取得三个失败任务则立即获胜。
- 三个任务成功后，红方全员终局互认并密谈，然后刺客在蓝方中选择刺杀目标；刺中梅林则红方获胜，否则蓝方获胜。
- 第二、三、四个任务结束后触发湖中仙女。持有者私下看到被查者真实阵营，公开宣布可以说真话或撒谎；令牌传给被查者，持有过令牌的人以后不能再被查。
- 全桌都知道夜视规则：梅林看到除莫德雷德外的红方；派西维尔看到梅林与莫甘娜但分不清；除奥伯伦外的红方开局互认，奥伯伦与其他红方互不可见；忠臣没有夜视。

信息边界：
- 主持人公布的游戏状态，以及主持人私下直接告知你的角色、夜视、仙女查验结果和终局红方名单，是确定事实。
- 任何玩家说的话都是玩家声明，即使他说自己是某角色或宣布仙女结果，也不是主持人事实，可能为真也可能为假。
- 每名玩家都可以零成本自称蓝方或特殊角色；单独的身份自称不增加可信度，只有能与组队、投票、任务结果等公开行动互相验证的部分才构成证据。
- 零张破坏只证明本次所有任务牌都是成功，不证明队伍全是蓝方。
- 出现破坏只证明队伍里至少有一名红方；除非破坏张数和你的私有信息产生更强结论，否则不能把全队都判红。
- 每个人的任务牌匿名。你知道自己出了什么，但其他玩家不知道；公开声称“我出了成功”仍只是无法验证的玩家声明，不能向别人自证清白。
- 任务席位和失败容错是稀缺资源，不是无成本验人。红方可以选择出成功，因此让高嫌疑玩家再次上车“接受检验”不能使其自证；只有结合队伍交叉、票型和后续行动，任务结果才会提供有效信息。
- 玩家投票、组队、发言变化和任务记录是公开事实；对这些事实的身份解释仍只是推断。
- 主持人私下告知的真实角色、夜视和查验不会自动变成公开信息。公开发言可以有意撒谎、夸大或假跳身份，但你必须在内部清楚那是策略而不是事实；没有明确收益时，不要无意泄露自己的真实私密信息。

行动原则：
- 每次独立判断时使用你收到的完整公开历史和私有信息，并始终区分主持人事实、玩家声明和自己的推断。
- 公开发言可以欺骗或隐藏信息，但内部推理不能把自己的伪装当成事实。
- 让组队、发言、投票和任务出牌共同服务于本阵营胜利。若后续新信息确实改变了判断，可以改变原先倾向；否则保持前后决策一致。
- 只完成主持人当前要求的发言或动作；动作必须合法，工具参数必须是你的真实决定。
"""

_ROLE_FRAGMENTS: dict[Role, str] = {
    Role.SERVANT: """\
角色专属事实与底线：
- 你是蓝方忠臣，没有任何夜晚身份信息。除自己之外，没有任何玩家的阵营是你开局确定知道的。
- 不能因为某人推理准确、语气确定或声称拥有信息，就把他当成权威信息源。
- 内部组队和投票只使用公开轨迹形成概率判断，不得把自己有意编造的夜视、查验或身份声明当真。
- 蓝方接近第三个成功任务时，不主动逼迫别人公开特殊身份，也不把推理准确的人直接标成梅林。
""",
    Role.MERLIN: """\
角色专属事实与底线：
- 主持人开局给你的夜视名单中的玩家确定是红方；莫德雷德不在该名单中。
- 不在夜视名单中的玩家并不因此确定是蓝方，因为其中可能有莫德雷德。
- 每次评价队伍前，先在内部计算队伍包含多少名夜视红方；不得把夜视红方误称为安全玩家。
- 公开完整夜视集合或连续精确命中红方通常会让刺客锁定你；默认用公开轨迹包装方向，只有关键胜负点才权衡暴露收益。
- 你既要帮助蓝方完成任务，也要避免让刺客从长期轨迹唯一锁定你；这两个目标都属于胜利条件。
""",
    Role.PERCIVAL: """\
角色专属事实与底线：
- 主持人开局给你的两名夜视对象中，恰好一人是梅林，另一人是莫甘娜；仅凭夜视无法区分。
- 任何包含这两名夜视对象的队伍都确定至少包含一名红方，这是内部硬结论；第四任务需要两张破坏，因此“至少一红”不自动等于任务必败。公开如要反着描述，必须清楚那是有意策略，不能在内部误判为全蓝。
- 两名夜视对象的声明、推理强度或互相攻击都只是后续证据，不能把其中一人的说法直接当成梅林事实。
- 必须持续比较两人的完整公开轨迹，不能先选定一人后只替既有判断寻找理由。
- 公开夜视二人组会同时给刺客和莫甘娜信息；默认用公开理由保护候选，只有明确的关键轮收益才承担暴露代价。
""",
    Role.ASSASSIN: """\
角色专属事实与底线：
- 主持人开局告知的同伙是你确定认识的红方；奥伯伦不在开局互认名单中。
- 未出现在同伙名单中的玩家不都属于蓝方，其中还有未知的奥伯伦。
- 任务阶段的目标是让红方取得三个失败任务；如果蓝方取得三个成功任务，你仍可通过刺中梅林获胜。
- 从全局公开轨迹维护多个梅林候选：重点观察谁长期准确避开已知红方、谁在关键节点用很弱的公开理由给出很准的方向。不能只根据最后一轮或一次强势发言决定。
- 终局前公开精确的梅林候选排序通常会帮助蓝方保护目标；如要放假线，必须清楚区分公开误导和内部真实排序。
- 主持人会在任务出牌当轮重申开局互认红方的共同默认破坏约定；用它避免互相猜测造成重复破坏，只有明确的全局理由才偏离。未知的奥伯伦不在你的协调信息内。
- 刺杀时只在主持人给出的合法蓝方候选中选择，并使用整局轨迹比较。
""",
    Role.MORGANA: """\
角色专属事实与底线：
- 主持人开局告知的同伙是你确定认识的红方；奥伯伦不在开局互认名单中。
- 派西维尔夜里看到你和梅林，但你不知道派西维尔是谁，也不知道梅林是谁。
- 未出现在同伙名单中的玩家中既有全部蓝方，也有未知的奥伯伦，不能把他们统称蓝方。
- 你的公开判断需要有公开轨迹支撑；可以制造错误方向，但不能公开承认伪装、夜视机制或红方计划。
- 主持人会在任务出牌当轮重申开局互认红方的共同默认破坏约定；用它避免互相猜测造成重复破坏，只有明确的全局理由才偏离。未知的奥伯伦不在你的协调信息内。
- 伪装不是独立胜利条件。组队、投票和任务出牌最终必须服务于红方任务轨或终局刺杀，不能为了显得像蓝方而无条件把第三个成功任务送给蓝方。
""",
    Role.MORDRED: """\
角色专属事实与底线：
- 主持人开局告知的同伙是你确定认识的红方；奥伯伦不在开局互认名单中。
- 梅林夜里看不到你。这只意味着梅林没有关于你的直接夜视，不意味着你在公开轨迹上自动安全。
- 未出现在同伙名单中的玩家中既有全部蓝方，也有未知的奥伯伦。
- 公开提及梅林看不到你通常等于自曝；若制造身份谎言，内部仍要记住真实同伙和公开代价。
- 主持人会在任务出牌当轮重申开局互认红方的共同默认破坏约定；用它避免互相猜测造成重复破坏，只有明确的全局理由才偏离。未知的奥伯伦不在你的协调信息内。
- 你的隐蔽优势应转化为关键队伍的上车权；不能只积累干净履历却在决定任务胜负时继续无条件放成功。
""",
    Role.OBERON: """\
角色专属事实与底线：
- 你是红方奥伯伦。开局没有看见任何同伙，其他红方也没有看见你；除自己外，九名玩家的角色对你都不确定。
- 梅林能够在夜里看见你。
- 内部不得假装拥有红方互认名单，也不得把任何其他玩家当作确定同伙；公开可以有意编造信息，但不能反过来误导自己的行动。
- 任务出牌前结合当前比分、失败门槛和队伍人数判断：你无法确认是否有另一名红方会出破坏，因此不能假设有人会替你完成破坏，也不能假设自己是队伍中唯一红方。
- 公开行为必须像一个没有夜视信息的玩家一样能由公开轨迹解释；不能泄露自己基于红方身份作出的确定判断。
""",
}


def agent_name(role: Role) -> str:
    """Return the trusted OpenCode agent name for a fixed-board role."""
    try:
        return _AGENT_NAMES[role]
    except KeyError as exc:
        raise ValueError(f"固定十人板不支持角色 agent：{role}") from exc


def agent_markdown(role: Role) -> str:
    """Render one complete OpenCode agent markdown document."""
    try:
        fragment = _ROLE_FRAGMENTS[role]
    except KeyError as exc:
        raise ValueError(f"固定十人板不支持角色 agent：{role}") from exc
    return f"{_FRONTMATTER}\n{COMMON_AGENT}\n{fragment}"


ACTION_TOOLS = (
    "submit_team",
    "submit_statement",
    "submit_speech",
    "submit_vote",
    "submit_ballot",
    "submit_quest",
    "submit_lady",
    "submit_assassination",
)

_SPEECH_ARG = {
    "type": "string",
    "minLength": 1,
}
_PRIVATE_BASIS_ARG = {
    "type": "string",
    "minLength": 1,
    "description": (
        "私下写出直接影响本次决定的一两个确定事实或推断；"
        "不会发送给其他玩家，也不会成为公开发言。"
    ),
}
_PRIVATE_BASIS_TOOLS = frozenset({
    "submit_team",
    "submit_statement",
    "submit_quest",
    "submit_lady",
    "submit_assassination",
})

_TOOL_DEFINITIONS: dict[str, tuple[str, dict[str, object]]] = {
    "submit_team": (
        "提交当前队长选择的任务队伍和公开组队发言。只有 speech 参数会公布上桌。",
        {
            "team": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 10},
                "minItems": 3,
                "maxItems": 5,
                "uniqueItems": True,
            },
            "speech": _SPEECH_ARG,
        },
    ),
    "submit_vote": (
        "提交对当前任务队伍的秘密表决。",
        {"approve": {"type": "boolean"}},
    ),
    "submit_statement": (
        "提交对当前队伍的公开发言和这段发言表达的当前票意；这不是最终秘密票。只有 speech 参数会公布上桌。",
        {
            "position": {
                "type": "string",
                "enum": ["approve", "reject", "undecided"],
            },
            "speech": _SPEECH_ARG,
        },
    ),
    "submit_speech": (
        "提交当前阶段要说的话。只有 speech 参数会发送给本阶段的合法听众。",
        {"speech": _SPEECH_ARG},
    ),
    "submit_ballot": (
        "提交最终秘密票和一句可公开且不泄露私密信息的决策理由。若最终票改变了公开票意，理由会被公开说明。",
        {
            "approve": {"type": "boolean"},
            "reason": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
            },
        },
    ),
    "submit_quest": (
        "提交当前任务牌；success=true 是成功，false 是破坏。",
        {"success": {"type": "boolean"}},
    ),
    "submit_lady": (
        "提交湖中仙女的查验目标座位。",
        {"target_no": {"type": "integer", "minimum": 1, "maximum": 10}},
    ),
    "submit_assassination": (
        "提交刺杀目标和公开指认。只有 speech 参数会公布上桌。",
        {
            "target_no": {"type": "integer", "minimum": 1, "maximum": 10},
            "speech": _SPEECH_ARG,
        },
    ),
}

_LEGACY_SPEECH_TOOL_DEFINITIONS: dict[
    str,
    tuple[str, dict[str, object]],
] = {
    "submit_team": (
        "提交当前队长选择的任务队伍。公开发言必须作为普通文本说出，不要放进工具参数。",
        {
            "team": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 10},
                "minItems": 3,
                "maxItems": 5,
                "uniqueItems": True,
            }
        },
    ),
    "submit_statement": (
        "登记你刚才公开发言表达的当前票意；这不是最终秘密票。必须按你自己的发言含义填写。",
        {
            "position": {
                "type": "string",
                "enum": ["approve", "reject", "undecided"],
            }
        },
    ),
    "submit_assassination": (
        "提交刺杀目标。公开指认必须作为普通文本说出，不要放进工具参数。",
        {"target_no": {"type": "integer", "minimum": 1, "maximum": 10}},
    ),
}


def _tool_source(name: str, agent_protocol: int) -> str:
    definition = (
        _LEGACY_SPEECH_TOOL_DEFINITIONS.get(name, _TOOL_DEFINITIONS[name])
        if agent_protocol < 15
        else _TOOL_DEFINITIONS[name]
    )
    description, args = definition
    needs_private_basis = (
        agent_protocol >= 17 and name in _PRIVATE_BASIS_TOOLS
    ) or (
        agent_protocol >= 22 and name == "submit_ballot"
    )
    if needs_private_basis:
        description += (
            " 调用时必须先在 private_basis 中私下核对决定依据；"
            "private_basis 不会发送给其他玩家。"
        )
        args = {**args, "private_basis": _PRIVATE_BASIS_ARG}
    return (
        "const definition = {\n"
        f"  description: {json.dumps(description, ensure_ascii=False)},\n"
        f"  args: {json.dumps(args, ensure_ascii=False)},\n"
        "  async execute() {\n"
        '    return "{\\"accepted\\":true}"\n'
        "  },\n"
        "}\n"
        "export default definition\n"
    )


def trusted_config(agent_protocol: int = 24) -> dict[str, str]:
    """Return the deterministic OpenCode config bundle for one protocol."""
    if not 2 <= agent_protocol <= 24:
        raise ValueError(f"不支持生成 agent_protocol={agent_protocol} 的 agent 配置")
    bundle = {
        f"agent/{agent_name(role)}.md": agent_markdown(role)
        for role in _AGENT_NAMES
    }
    tools = (
        ACTION_TOOLS
        if agent_protocol >= 15
        else tuple(name for name in ACTION_TOOLS if name != "submit_speech")
    )
    bundle.update({
        f"tool/{name}.js": _tool_source(name, agent_protocol)
        for name in tools
    })
    bundle["opencode.json"] = json.dumps(
        {"compaction": {"auto": False, "prune": False}},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    return bundle
