"""prompts：渲染冒烟与文案纪律——agent 后端没有离线整局测试，
prompt 构造是它在零 key 测试下唯一可验证的纯函数路径，这里保证：

- 任意角色的 system 都能渲染不炸，且只出现自己的身份、夜晚情报渲染成座位号；
- 各决策阶段的 OpenCode JSON 提问包含必要字段与 schema 说明；
- 第 5 次组队的"再否决红方直接获胜"警告按条件出现；
- 公开事件行只携带公开信息。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from avalon import prompts
from avalon.player import PlayerView
from avalon.state import ProposalRecord, Role
from core.threads import ThreadMessage

_LINEUP = [Role.MERLIN, Role.PERCIVAL, Role.SERVANT, Role.SERVANT, Role.SERVANT, Role.SERVANT,
           Role.ASSASSIN, Role.MORGANA, Role.MORDRED, Role.OBERON]


def _view(role: Role, *, attempt: int = 2) -> PlayerView:
    return PlayerView(
        seat_no=1, role=role, intel=[7, 8] if role is not Role.SERVANT else [],
        model=None, lineup=list(_LINEUP), players=10,
        leader_no=1, quest_no=2, attempt=attempt, team_size=3, fails_required=1,
        succeeded=1, failed=0, team=[1, 2, 3], lady_used=[5],
    )


@pytest.mark.parametrize("role", list(Role))
def test_system_renders_for_every_role(role: Role) -> None:
    """system 是发牌时刻固定的全部：规则 + 阵容 + 座次 + 自己的身份。"""
    text = prompts.system_text(_view(role))
    assert "你的角色：" in text and prompts._ROLE_LABEL[role] in text
    assert "本局角色阵容（公开）" in text
    assert "本局共 10 人，座位号 1~10" in text
    assert "本局任务表：M1 3人/1破坏失败" in text and "M5 5人/1破坏失败" in text
    assert "游戏基本功" in text and "任务成功不等于车上全蓝" in text
    assert f"{prompts._ROLE_LABEL[role]}硬约束" in text
    assert f"{prompts._ROLE_LABEL[role]}基本功" in text
    assert "你是 1 号" in text
    assert "简短干练" in text and "口语化的 2~3 句" in text
    assert text.startswith("你是一名《阿瓦隆》玩家。")  # 嗓音可缺省，身份句由 system 自带；网名绝不进 prompt


def test_system_injects_role_specific_playbook_only() -> None:
    """角色秘籍贴近身份注入：派西维尔拿反莫甘娜规则，普通忠臣不会拿到这条夜视职责。"""
    percival = prompts.system_text(_view(Role.PERCIVAL))
    servant = prompts.system_text(_view(Role.SERVANT))
    assert "莫甘娜切真梅林" in percival
    assert "莫甘娜切真梅林" not in servant
    assert "派西维尔基本功" in percival
    assert "派西维尔基本功" not in servant


def test_role_hard_rules_cover_identity_leak_red_lines() -> None:
    """角色硬约束单独存在：保护梅林、派西维尔和刺客底线，不只靠打法建议。"""
    merlin = prompts.system_text(_view(Role.MERLIN))
    assert "梅林硬约束" in merlin
    assert "公开 speech 不能说“我知道谁红/谁蓝”" in merlin
    assert "不能公开排出完整红坑" in merlin

    percival = prompts.system_text(_view(Role.PERCIVAL))
    assert "派西维尔硬约束" in percival
    assert "不能说出你夜里看到的两个候选" in percival
    assert "不能点名“谁是梅林、谁是莫甘娜”" in percival

    assassin = prompts.system_text(_view(Role.ASSASSIN))
    assert "刺客硬约束" in assassin
    assert "公开 speech 不要暴露你在找梅林" in assassin


def test_role_hard_rules_follow_lineup_variants() -> None:
    """梅林/派西维尔红线按本局阵容写，不把不存在的特殊角色塞进提示。"""
    simple = [Role.MERLIN, Role.PERCIVAL, Role.SERVANT, Role.ASSASSIN, Role.MINION]

    merlin = prompts.system_text(replace(_view(Role.MERLIN), lineup=simple))
    assert "莫德雷德你看不到" not in merlin
    assert "你看不到莫德雷德" not in merlin
    assert "夜里看到了红方名单" in merlin
    assert "本局没有莫德雷德" in merlin

    percival = prompts.system_text(replace(_view(Role.PERCIVAL), lineup=simple))
    assert "两个候选" not in percival
    assert "谁是梅林、谁是莫甘娜" not in percival
    assert "莫甘娜切真梅林" not in percival
    assert "不能点出你夜里看到的人就是梅林" in percival
    assert "你夜里看到的是梅林本人" in percival


def test_night_intel_rendered_as_seats() -> None:
    """知情角色的任务书里要出现情报座位号；忠臣的任务书声明无情报。"""
    assert "7号、8号" in prompts._role_brief(_view(Role.MERLIN))
    assert "没有任何夜晚情报" in prompts._role_brief(_view(Role.SERVANT))


def test_hammer_warning_only_on_fifth_attempt() -> None:
    """第 5 次组队渲染"再否决红方直接获胜"警告，平时不渲染。"""
    assert "红方直接获胜" in prompts.situation(_view(Role.SERVANT, attempt=5))
    assert "红方直接获胜" not in prompts.situation(_view(Role.SERVANT, attempt=2))
    text = prompts.situation(_view(Role.SERVANT, attempt=2))
    assert "前情回顾" in text and "M4 5人/2破坏失败" in text and "M5 5人/1破坏失败" in text
    assert "已否决 1 次" in text and "蓝方 1 : 0 红方" in text
    assert "公开账本：暂无历史提案" in text


def test_situation_marks_player_claims_as_uncertain() -> None:
    """每轮前情回顾提醒事实分层：只把系统事实和自己的私有视角当确定事实。"""
    text = prompts.situation(_view(Role.SERVANT))
    assert "系统公开事实和你自己的私有视角才是确定事实" in text
    assert "身份声明、任务出牌自述、湖中仙女公布结果" in text
    assert "只是玩家声称，可能为真也可能为假" in text
    assert "不能当作事实前提" in text


def test_situation_carries_public_vote_ledger() -> None:
    """前情回顾带公开账本：历史队伍、逐席票型、通过/否决与任务结果都可见。"""
    view = _view(Role.SERVANT)
    view.history = [
        ProposalRecord(quest=1, attempt=1, leader_no=3, team=[1, 3, 5], votes={1: True, 2: False, 3: True}, approved=False),
        ProposalRecord(
            quest=1, attempt=2, leader_no=4, team=[2, 4, 6], votes={1: True, 2: True, 3: False},
            approved=True, fails=1, result="fail",
        ),
    ]
    text = prompts.situation(view)
    assert "M1.1 队长3号 队伍[1号、3号、5号]" in text
    assert "票型：1号赞成、2号反对、3号赞成；结果：否决" in text
    assert "M1.2 队长4号 队伍[2号、4号、6号]" in text
    assert "结果：通过，任务失败，破坏票 1 张" in text


def test_opencode_asks_carry_required_keys() -> None:
    """各决策阶段 JSON 提问包含必要字段说明。"""
    view = _view(Role.SERVANT)
    propose = prompts.opencode_ask("propose", view)
    assert '"phase": "propose"' in propose and '"team"' in propose and '"speech"' in propose and '"monologue"' in propose

    discuss = prompts.opencode_ask("discuss", view)
    assert '"phase": "discuss"' in discuss and '"speech"' in discuss

    vote = prompts.opencode_ask("team_vote", view)
    assert '"phase": "team_vote"' in vote and '"approve"' in vote

    quest = prompts.opencode_ask("quest", view)
    assert '"phase": "quest"' in quest and '"success"' in quest

    assassinate = prompts.opencode_ask("assassinate", view)
    assert '"phase": "assassinate"' in assassinate and '"target_no"' in assassinate

    lady_choose = prompts.opencode_ask("lady_choose", view)
    assert '"phase": "lady_choose"' in lady_choose and '"target_no"' in lady_choose

    lady_announce = prompts.opencode_ask("lady_announce", replace(_view(Role.MERLIN, attempt=2), lady_target=3, lady_seen="evil"))
    assert '"phase": "lady_announce"' in lady_announce and '"speech"' in lady_announce

    banter = prompts.opencode_ask("banter", view)
    assert '"phase": "banter"' in banter and '"speech"' in banter


def test_opencode_asks_include_private_self_check() -> None:
    """每阶段都要求输出前自检，并允许 OpenCode 子 agent 可用时做合法性/泄密审稿。"""
    text = prompts.opencode_ask("propose", _view(Role.MERLIN))
    assert "输出前自检（只在心里做）" in text
    assert "子 agent" in text
    assert "检查合法性、泄密风险和 JSON 格式" in text
    assert "最终只输出一个 JSON 对象" in text
    assert "不要写出自检过程" in text


def test_opencode_prompt_is_natural_host_handoff() -> None:
    """发给 OpenCode 的整包文本像主持人私聊转述，不暴露 LangChain role 标签。"""
    view = _view(Role.MERLIN)
    view.prompt = prompts.human_message(["主持人宣布开局。"], "局面", prompts.opencode_ask("discuss", view))
    text = prompts.opencode_prompt(view)
    assert "我是这局《阿瓦隆》的主持人，现在通过你的私人频道给你传话" in text
    assert "【你的固定设定】" in text and "【现在轮到你】" in text
    assert "你此前还没有私人行动记录" in text
    assert "[system]" not in text and "[user]" not in text and "[assistant]" not in text

    view.messages = [
        ThreadMessage(role="human", content="上一轮问题"),
        ThreadMessage(role="assistant", content='{"speech":"上一轮发言","monologue":"上一轮内心"}'),
    ]
    text = prompts.opencode_prompt(view)
    assert "主持人此前对你说" in text
    assert "你此前回复" in text
    assert "上一轮发言" in text


def test_opencode_prompt_incremental_after_session_seed() -> None:
    """OpenCode session 已经种过上下文后，后续只发本轮增量，不再整包重喂固定设定和历史。"""
    view = _view(Role.MERLIN)
    view.prompt = prompts.human_message(["新事件"], "新局面", prompts.opencode_ask("discuss", view))
    text = prompts.opencode_prompt(view, include_context=False)
    assert "沿用本 session 开局时给你的身份" in text
    assert "【现在轮到你】" in text and "新事件" in text
    assert "【你的固定设定】" not in text
    assert "【你的私人历史】" not in text
    assert "你的角色：梅林" not in text


def test_opencode_ask_constrains_good_and_frees_evil() -> None:
    """蓝方出牌提问写死只能成功；红方给出可破坏的事实与门槛。"""
    good = prompts.opencode_ask("quest", _view(Role.SERVANT))
    assert "你只能出成功" in good
    evil = prompts.opencode_ask("quest", _view(Role.ASSASSIN))
    assert "可出成功或破坏" in evil and "1 张" in evil


def test_lady_announce_ask_carries_secret_and_lying_right() -> None:
    """宣布提问含真实阵营与"可撒谎"的规则说明。"""
    view = replace(_view(Role.MERLIN), lady_target=3, lady_seen="evil")
    text = prompts.opencode_ask("lady_announce", view)
    assert "红方" in text and "真实阵营" in text


def test_human_message_composition() -> None:
    """human 消息 = 事件块（可空）+ 局面（可空）+ 风格提醒（嗓音逐轮重申）+ 压轴的提问
    （JSON 格式要求必须是最末位的锚，否则模型照提醒的体例回话、不出 JSON）。"""
    full = prompts.human_message(["甲", "乙"], "局面", "提问", "你说话阴阳怪气。")
    assert full.index("甲") < full.index("乙") < full.index("局面") < full.index("风格提醒") < full.index("提问")
    assert "你说话阴阳怪气。" in full
    assert "说话简短干练，只打 1~2 个重点" in full
    assert "别写长段总结，别复述全桌" in full
    bare = prompts.human_message([], "", "提问")
    assert bare.endswith("提问") and "风格提醒" in bare
    assert "说话简短干练" in bare


def test_event_lines_render_public_facts_only() -> None:
    """公开事件行冒烟：票型/张数/发言上桌，函数签名里压根没有秘密可带。"""
    intro = prompts.line_host_propose_intro(2, 1, 3, 3)
    assert "第二个任务" in intro and "队长三号" in intro and "第一次" not in intro  # 首次组队不报次数
    assert "最后机会" in prompts.line_host_propose_intro(2, 5, 3, 3)
    record = prompts.line_team_record(3, [1, 3, 5])
    assert "队长 3号" in record and "1号、3号、5号" in record
    votes = prompts.line_votes({1: True, 2: False, 3: True})
    assert "1号✔" in votes and "2号✘" in votes
    approved = prompts.line_host_vote_result(2, 3, True, 0)
    assert "二票赞成" in approved and "一票反对" in approved and "通过" in approved
    rejected = prompts.line_host_vote_result(1, 3, False, 2, next_leader=4)
    assert "否决" in rejected and "下一轮由四号提队" in rejected
    quest = prompts.line_host_quest_result(2, 1, "fail", 1, 1, next_leader=5)
    assert "失败" in quest and "一张破坏票" in quest and "蓝方一比一" in quest and "下一轮由五号提队" in quest
    saved = prompts.line_host_quest_result(4, 1, "success", 2, 1)
    assert "未达到门槛" in saved and "成功" in saved
    lady = prompts.line_host_lady(3, 5)
    assert "三号查验五号" in lady and "令牌传给五号" in lady
    assert prompts.line_player(7, "走起") == "7号：「走起」"


def test_host_lines_are_tts_friendly() -> None:
    """主持词是要逐句 TTS 的口播文案：不带【】标记、✔✘ 符号与 ×N 计数记法，
    阵容用"四名忠臣"这类口语数法；玩家发言（带引号）绝不出现在主持词里。"""
    samples = [
        prompts.line_host_opening(10, 4, 5, 3),
        prompts.line_host_deal(3, 2),
        prompts.line_host_propose_intro(1, 2, 3, 6),
        prompts.line_host_vote_result(6, 10, True, 0),
        prompts.line_host_quest_result(1, 0, "success", 1, 0),
        prompts.line_host_lady(2, 7),
        prompts.line_host_assassination(10, 2, True),
        prompts.line_host_reveal("evil", "assassination", {1: Role.MERLIN, 2: Role.ASSASSIN}),
        prompts.line_host_closing(),
    ]
    for text in samples:
        for banned in ("【", "】", "✔", "✘", "×", "「", "」"):
            assert banned not in text, f"主持词含不可念符号 {banned!r}：{text}"
    assert "十人局" in samples[0] and "队长是五号" in samples[0]


def test_rules_contain_no_coaching() -> None:
    """中性化纪律：公共 RULES 只有规则与对局方式，不含打法心法/秘籍。"""
    for banned in ("心法", "纪律", "打法", "苟", "布饵"):
        assert banned not in prompts.RULES
