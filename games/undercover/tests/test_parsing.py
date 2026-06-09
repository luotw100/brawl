"""parsing.extract_json_object / parse_json：从模型自由文本里稳健抽 JSON。

为什么要稳健：思考型模型会在 JSON 前后吐推理/围栏/尾巴文字，且各厂商格式不一。这里
不依赖 with_structured_output，只靠花括号配对 + pydantic 校验——只要模型能吐文本就能用。
"""

from __future__ import annotations

import pytest

from undercover.parsing import JsonParseError, parse_json
from undercover.state import Describe, Vote


def test_clean_json() -> None:
    """干净的 JSON 对象直接解析。"""
    out = parse_json('{"description": "一种水果", "monologue": "稳住"}', Describe)
    assert out.description == "一种水果"
    assert out.monologue == "稳住"


def test_code_fence() -> None:
    """```json 代码围栏里的对象也能抽出来。"""
    text = '```json\n{"description": "d", "monologue": "m"}\n```'
    assert parse_json(text, Describe).description == "d"


def test_reasoning_prefix_and_trailing_text() -> None:
    """思考模型在 JSON 前的大段推理 + JSON 后的尾巴文字都要能容忍。"""
    text = '让我想想……我应该说得模糊点。{"description": "d", "monologue": "m"} 好了就这样。'
    out = parse_json(text, Describe)
    assert out.description == "d"


def test_braces_inside_string_value() -> None:
    """字符串值里的花括号不能干扰配对扫描（不能在 '{' 处提前收尾）。"""
    text = '{"description": "这东西像 {大括号} 一样", "monologue": "m"}'
    assert parse_json(text, Describe).description == "这东西像 {大括号} 一样"


def test_type_coercion_on_target_no() -> None:
    """模型把整数写成字符串 "3"，pydantic 宽松模式矫正回 int。"""
    out = parse_json('{"target_no": "3", "reason": "r", "monologue": "m"}', Vote)
    assert out.target_no == 3
    assert isinstance(out.target_no, int)


def test_no_json_raises() -> None:
    """文本里没有 JSON（纯散文）→ 抽不出对象，抛 JsonParseError（不静默兜底成空对象）。"""
    with pytest.raises(JsonParseError):
        parse_json("我就是不给你 JSON", Describe)


def test_empty_output_raises() -> None:
    """空响应 → 抽不出对象，仍 fail-fast 抛 JsonParseError（绝不变成空对象）。"""
    with pytest.raises(JsonParseError):
        parse_json("", Describe)


def test_missing_field_raises() -> None:
    """缺必填字段（monologue）→ pydantic 校验失败，抛 JsonParseError。"""
    with pytest.raises(JsonParseError):
        parse_json('{"description": "d"}', Describe)


def test_fullwidth_quote_terminator_recovered() -> None:
    """模型用全角引号 ”} 收尾、把直引号丢了——严格扫描会判花括号不配对，json_repair 救回。"""
    text = '{"description": "动车很安静。", "monologue": "我的词没问题，继续保持。”}'
    out = parse_json(text, Describe)
    assert out.description == "动车很安静。"


def test_wrong_closing_bracket_recovered() -> None:
    """把闭合 } 误写成 ) —— 严格扫描扫到结尾仍未归零，json_repair 救回。"""
    text = '{"target_no": 3, "reason": "矛盾明显", "monologue": "跟票投最跳的那个。")'
    out = parse_json(text, Vote)
    assert out.target_no == 3


def test_truncated_object_recovered() -> None:
    """被截断少了右括号（只开不合）→ json_repair 补全救回（截断不再直接判负）。"""
    out = parse_json('{"description": "d", "monologue": "m"', Describe)
    assert out.description == "d"
    assert out.monologue == "m"
