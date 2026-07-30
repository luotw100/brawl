from __future__ import annotations

import pytest

from brawl.games.avalon.agents import (
    ACTION_TOOLS,
    COMMON_AGENT,
    agent_markdown,
    agent_name,
    trusted_config,
)
from brawl.games.avalon.state import Role

_FIXED_BOARD_ROLES = (
    Role.SERVANT,
    Role.MERLIN,
    Role.PERCIVAL,
    Role.ASSASSIN,
    Role.MORGANA,
    Role.MORDRED,
    Role.OBERON,
)


def test_all_fixed_board_roles_have_primary_agents_with_common_prefix() -> None:
    documents = [agent_markdown(role) for role in _FIXED_BOARD_ROLES]

    assert len({agent_name(role) for role in _FIXED_BOARD_ROLES}) == 7
    assert all(document.startswith("---\n") for document in documents)
    assert all("\nmode: primary\n" in document for document in documents)
    assert all(COMMON_AGENT in document for document in documents)


def test_minion_is_not_supported_by_fixed_board_agents() -> None:
    with pytest.raises(ValueError, match="固定十人板不支持角色 agent"):
        agent_name(Role.MINION)
    with pytest.raises(ValueError, match="固定十人板不支持角色 agent"):
        agent_markdown(Role.MINION)


def test_common_agent_guards_epistemics_and_vote_consistency() -> None:
    assert "玩家声明" in COMMON_AGENT
    assert "不是主持人事实" in COMMON_AGENT
    assert "单独的身份自称不增加可信度" in COMMON_AGENT
    assert "零张破坏只证明本次所有任务牌都是成功，不证明队伍全是蓝方" in COMMON_AGENT
    assert "公开声称“我出了成功”仍只是无法验证的玩家声明" in COMMON_AGENT
    assert "不是无成本验人" in COMMON_AGENT
    assert "接受检验”不能使其自证" in COMMON_AGENT
    assert "若后续新信息确实改变了判断，可以改变原先倾向" in COMMON_AGENT
    assert "否则保持前后决策一致" in COMMON_AGENT
    assert "不要换词复述" not in COMMON_AGENT
    assert "只说“过。”" not in COMMON_AGENT


def test_role_agents_contain_their_decisive_hard_rules() -> None:
    assert "没有任何夜晚身份信息" in agent_markdown(Role.SERVANT)
    assert "莫德雷德不在该名单中" in agent_markdown(Role.MERLIN)
    assert "任何包含这两名夜视对象的队伍都确定至少包含一名红方" in agent_markdown(Role.PERCIVAL)
    assert "使用整局轨迹比较" in agent_markdown(Role.ASSASSIN)
    assert "你不知道派西维尔是谁，也不知道梅林是谁" in agent_markdown(Role.MORGANA)
    assert "不意味着你在公开轨迹上自动安全" in agent_markdown(Role.MORDRED)
    assert "不得假装拥有红方互认名单" in agent_markdown(Role.OBERON)


def test_recognized_evil_agents_receive_shared_sabotage_playbook() -> None:
    for role in (Role.ASSASSIN, Role.MORGANA, Role.MORDRED):
        document = agent_markdown(role)
        assert "共同默认破坏约定" in document
        assert "避免互相猜测造成重复破坏" in document
        assert "只有明确的全局理由才偏离" in document
        assert "未知的奥伯伦不在你的协调信息内" in document
    assert "协调信息" not in agent_markdown(Role.OBERON)


def test_agents_do_not_contain_delivery_or_formatting_distractions() -> None:
    forbidden = ("观众", "人设", "口吻", "内心戏", "StructuredOutput")

    for role in _FIXED_BOARD_ROLES:
        document = agent_markdown(role)
        assert all(word not in document for word in forbidden)


def test_trusted_config_contains_locked_agents_pure_action_tools_and_no_compaction() -> None:
    bundle = trusted_config()

    assert {path for path in bundle if path.startswith("agent/")} == {
        f"agent/{agent_name(role)}.md"
        for role in _FIXED_BOARD_ROLES
    }
    assert {path for path in bundle if path.startswith("tool/")} == {
        f"tool/{name}.js"
        for name in ACTION_TOOLS
    }
    for role in _FIXED_BOARD_ROLES:
        document = bundle[f"agent/{agent_name(role)}.md"]
        assert '"*": deny' in document
        assert "temperature:" not in document
    for name in ACTION_TOOLS:
        source = bundle[f"tool/{name}.js"]
        assert "fetch(" not in source
        assert "Bun.file" not in source
        assert "execute()" in source
    assert '"auto": false' in bundle["opencode.json"]
    assert '"prune": false' in bundle["opencode.json"]


def test_public_speech_is_an_explicit_tool_argument() -> None:
    bundle = trusted_config()

    for name in (
        "submit_team",
        "submit_statement",
        "submit_speech",
        "submit_assassination",
    ):
        source = bundle[f"tool/{name}.js"]
        assert '"speech"' in source
        assert "只有 speech 参数" in source


def test_pre_v15_config_keeps_the_legacy_speech_boundary() -> None:
    bundle = trusted_config(14)

    assert "tool/submit_speech.js" not in bundle
    for name in ("submit_team", "submit_statement", "submit_assassination"):
        source = bundle[f"tool/{name}.js"]
        assert '"speech"' not in source
    assert "普通文本" in bundle["tool/submit_team.js"]
    assert "刚才公开发言" in bundle["tool/submit_statement.js"]


def test_v17_strategic_tools_require_a_private_basis() -> None:
    latest = trusted_config(17)
    previous = trusted_config(16)

    for name in (
        "submit_team",
        "submit_statement",
        "submit_quest",
        "submit_lady",
        "submit_assassination",
    ):
        assert '"private_basis"' in latest[f"tool/{name}.js"]
        assert "不会发送给其他玩家" in latest[f"tool/{name}.js"]
        assert '"private_basis"' not in previous[f"tool/{name}.js"]
    for name in ("submit_speech", "submit_vote", "submit_ballot"):
        assert '"private_basis"' not in latest[f"tool/{name}.js"]


def test_v22_final_ballot_requires_a_private_basis_without_changing_old_tools() -> None:
    previous = trusted_config(21)
    latest = trusted_config(22)

    assert '"private_basis"' not in previous["tool/submit_ballot.js"]
    assert '"private_basis"' in latest["tool/submit_ballot.js"]
    assert "不会发送给其他玩家" in latest["tool/submit_ballot.js"]
    assert previous["tool/submit_team.js"] == latest["tool/submit_team.js"]
