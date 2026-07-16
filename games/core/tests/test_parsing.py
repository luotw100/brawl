"""core.parsing 的两道解析路径：严格优先，json_repair 兜底救脏输出，无对象 fail-fast。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from core.parsing import JsonParseError, extract_json_object, parse_json


class _Card(BaseModel):
    approve: bool
    monologue: str


def test_clean_json_parses_verbatim() -> None:
    card = parse_json('{"approve": true, "monologue": "好"}', _Card)
    assert card.approve is True and card.monologue == "好"


def test_json_inside_prose_and_fence() -> None:
    text = '让我想想……\n```json\n{"approve": false, "monologue": "坏"}\n```\n就这样。'
    card = parse_json(text, _Card)
    assert card.approve is False


def test_braces_inside_strings_are_ignored() -> None:
    card = parse_json('{"approve": true, "monologue": "这个{括号}不影响"}', _Card)
    assert card.monologue == "这个{括号}不影响"


def test_fullwidth_quotes_repaired() -> None:
    # json_repair 能把全角引号定界的脏 JSON 救成一个对象（值可能带残字，契约是"能救则救"）
    card = parse_json('{"approve": true, "monologue": ”脏引号“}', _Card)
    assert card.approve is True and "脏引号" in card.monologue


def test_trailing_comma_repaired() -> None:
    card = parse_json('{"approve": true, "monologue": "尾逗号",}', _Card)
    assert card.monologue == "尾逗号"


def test_key_with_chinese_quote_cleaned() -> None:
    card = parse_json('{"approve": true, "monologue」: "键名脏了"}', _Card)
    assert card.monologue == "键名脏了"


def test_empty_output_fails_fast() -> None:
    with pytest.raises(JsonParseError):
        parse_json("", _Card)


def test_pure_prose_fails_fast() -> None:
    with pytest.raises(JsonParseError):
        parse_json("我完全不知道该怎么选，随便吧。", _Card)


def test_unbalanced_braces_fails() -> None:
    with pytest.raises(JsonParseError):
        extract_json_object('{"approve": true, "monologue": "没闭合')


def test_schema_violation_fails() -> None:
    with pytest.raises(JsonParseError, match="字段校验失败"):
        parse_json('{"approve": "也许", "monologue": "x"}', _Card)


def test_missing_required_field_fails() -> None:
    with pytest.raises(JsonParseError):
        parse_json('{"approve": true}', _Card)
