"""prompts：渲染冒烟与文案纪律——LLM 后端没有离线整局测试，
prompt 构造是它在零 key 测试下唯一可验证的纯函数路径，这里保证：

- 任意角色的 system 都能渲染不炸，且只出现自己的身份、夜晚情报渲染成座位号；
- 各决策提问的标签行（内心戏/发言/投票/队伍/出牌/刺杀/查验）齐全；
- 第 5 次组队的"再否决红方直接获胜"警告按条件出现；
- 公开事件行只携带公开信息；extractor 提示包住原文并给出 JSON 形状。
"""

from __future__ import annotations

import pytest

from avalon import prompts
from avalon.player import PlayerView
from avalon.state import ProposalRecord, Role

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


def test_asks_carry_label_lines() -> None:
    """各决策提问的标签行齐全——标签行是公开/私密边界，也是 extractor 的锚点。"""
    propose = prompts.ask_propose(3)
    assert "内心戏：" in propose and "发言：" in propose and "队伍：" in propose
    assert "投票：" in prompts.ask_vote([1, 2, 3]) and "内心戏：" in prompts.ask_vote([1, 2, 3])
    assert "出牌：" in prompts.ask_quest(True, 1)
    assert "刺杀：" in prompts.ask_assassinate([9, 10], [1, 2, 3])
    assert "查验：" in prompts.ask_lady_choose([2, 3])
    assert "发言：" in prompts.ask_discuss([1, 2, 3])
    assert "发言：" in prompts.ask_lady_announce(3, "evil")


def test_quest_ask_constrains_good_and_frees_evil() -> None:
    """蓝方出牌提问写死只能成功；红方给出可破坏的事实与门槛。"""
    assert "只能出成功" in prompts.ask_quest(True, 1)
    evil = prompts.ask_quest(False, 2)
    assert "可出成功或破坏" in evil and "2 张" in evil


def test_lady_announce_ask_carries_secret_and_lying_right() -> None:
    """宣布提问含真实阵营与"可撒谎"的规则说明。"""
    text = prompts.ask_lady_announce(3, "evil")
    assert "红方" in text and "可如实也可撒谎" in text


def test_human_message_composition() -> None:
    """human 消息 = 事件块（可空）+ 局面（可空）+ 风格提醒（嗓音逐轮重申）+ 压轴的提问
    （标签行格式要求必须是最末位的锚，否则模型照提醒的体例回话、不出标签行）。"""
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


def test_extract_prompts_wrap_raw_and_spec_json() -> None:
    """extractor 提示：原文包在 <<<>>> 里，给出字段形状与候选约束。"""
    raw = "随便想想\n内心戏：哈\n发言：上车\n队伍：1、3、5"
    p = prompts.extract_propose(raw, 3, 10)
    assert "<<<" in p and raw in p and '"team"' in p and "3 个互不相同" in p
    v = prompts.extract_vote("投票：赞成")
    assert '"approve"' in v
    a = prompts.extract_assassinate("刺杀：4", [1, 4, 6])
    assert '"target_no"' in a and "1、4、6" in a
