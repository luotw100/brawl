"""parsing.extract_json_object / parse_json：从模型自由文本里稳健抽 JSON。

为什么要稳健：思考型模型会在 JSON 前后吐推理/围栏/尾巴文字，且各厂商格式不一。这里
不依赖 with_structured_output，只靠花括号配对 + pydantic 校验——只要模型能吐文本就能用。
目标模型用阿瓦隆自己的决策结构（Statement/TeamVote/Proposal/Assassination）。
"""

from __future__ import annotations

import pytest

from avalon.parsing import JsonParseError, parse_json
from avalon.state import Assassination, Proposal, Statement, TeamVote


def test_clean_json() -> None:
    """干净的 JSON 对象直接解析。"""
    out = parse_json('{"speech": "这队我信", "monologue": "稳住"}', Statement)
    assert out.speech == "这队我信"
    assert out.monologue == "稳住"


def test_code_fence() -> None:
    """```json 代码围栏里的对象也能抽出来。"""
    text = '```json\n{"speech": "s", "monologue": "m"}\n```'
    assert parse_json(text, Statement).speech == "s"


def test_reasoning_prefix_and_trailing_text() -> None:
    """思考模型在 JSON 前的大段推理 + JSON 后的尾巴文字都要能容忍。"""
    text = '让我想想……这队有点可疑。{"approve": false, "monologue": "m"} 好了就这样。'
    out = parse_json(text, TeamVote)
    assert out.approve is False


def test_braces_inside_string_value() -> None:
    """字符串值里的花括号不能干扰配对扫描（不能在 '{' 处提前收尾）。"""
    text = '{"speech": "这队像 {大括号} 一样怪", "monologue": "m"}'
    assert parse_json(text, Statement).speech == "这队像 {大括号} 一样怪"


def test_type_coercion_on_numbers_and_bools() -> None:
    """模型把数字写成字符串，pydantic 宽松模式矫正回 int；布尔同理。"""
    out = parse_json('{"team": ["0", "2"], "speech": "s", "monologue": "m"}', Proposal)
    assert out.team == [0, 2]
    shot = parse_json('{"target_no": "3", "speech": "s", "monologue": "m"}', Assassination)
    assert shot.target_no == 3 and isinstance(shot.target_no, int)


def test_no_json_raises() -> None:
    """文本里没有 JSON（纯散文）→ 抽不出对象，抛 JsonParseError（不静默兜底成空对象）。"""
    with pytest.raises(JsonParseError):
        parse_json("我就是不给你 JSON", Statement)


def test_empty_output_raises() -> None:
    """空响应 → 抽不出对象，仍 fail-fast 抛 JsonParseError（绝不变成空对象）。"""
    with pytest.raises(JsonParseError):
        parse_json("", Statement)


def test_missing_field_raises() -> None:
    """缺必填字段（monologue）→ pydantic 校验失败，抛 JsonParseError。"""
    with pytest.raises(JsonParseError):
        parse_json('{"speech": "s"}', Statement)


def test_fullwidth_quote_terminator_recovered() -> None:
    """模型用全角引号 ”} 收尾、把直引号丢了——严格扫描会判花括号不配对，json_repair 救回。"""
    text = '{"speech": "我支持这队。", "monologue": "队里没我，先看戏。”}'
    out = parse_json(text, Statement)
    assert out.speech == "我支持这队。"


def test_wrong_closing_bracket_recovered() -> None:
    """把闭合 } 误写成 ) —— 严格扫描扫到结尾仍未归零，json_repair 救回。"""
    text = '{"target_no": 3, "speech": "就是你", "monologue": "赌一把。")'
    out = parse_json(text, Assassination)
    assert out.target_no == 3


def test_truncated_object_recovered() -> None:
    """被截断少了右括号（只开不合）→ json_repair 补全救回（截断不再直接判负）。"""
    out = parse_json('{"speech": "s", "monologue": "m"', Statement)
    assert out.speech == "s"
    assert out.monologue == "m"
