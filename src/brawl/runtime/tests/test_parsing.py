"""brawl.runtime.parsing 的单一严格路径：完整 JSON object 入，任何修复或夹带都拒绝。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from brawl.runtime.parsing import JsonParseError, parse_json


class _Card(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    approve: bool
    monologue: str


class _Target(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    target_no: int


class _OtherwiseLooseTarget(BaseModel):
    target_no: int


class _MaybeCard(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    approve: bool | None


def test_clean_json_with_surrounding_whitespace_parses() -> None:
    card = parse_json(' \n {"approve": true, "monologue": "好"}\t', _Card)
    assert card.approve is True and card.monologue == "好"


def test_braces_inside_strings_are_plain_json_content() -> None:
    card = parse_json('{"approve": true, "monologue": "这个{括号}不影响"}', _Card)
    assert card.monologue == "这个{括号}不影响"


@pytest.mark.parametrize(
    "text",
    [
        '让我想想……\n```json\n{"approve": false, "monologue": "坏"}\n```',
        '{"approve": true, "monologue": ”脏引号“}',
        '{"approve": true, "monologue": "尾逗号",}',
        '{"approve": true, "monologue」: "键名脏了"}',
        '{"approve": true, "monologue": "草稿"}\n{"approve": false, "monologue": "最终"}',
        '{"approve": true, "monologue": "先答"} 后面还有解释',
    ],
)
def test_non_exact_or_repairable_output_is_rejected(text: str) -> None:
    with pytest.raises(JsonParseError, match="恰好"):
        parse_json(text, _Card)


@pytest.mark.parametrize(
    "text",
    [
        '{"approve": true, "approve": false, "monologue": "x"}',
        '{"approve": true, "monologue": NaN}',
    ],
)
def test_ambiguous_or_nonstandard_json_is_rejected(text: str) -> None:
    with pytest.raises(JsonParseError):
        parse_json(text, _Card)


@pytest.mark.parametrize("text", ["", "我完全不知道该怎么选，随便吧。", "[]", "null"])
def test_missing_json_object_fails_fast(text: str) -> None:
    with pytest.raises(JsonParseError):
        parse_json(text, _Card)


def test_bool_field_accepts_json_spelled_boolean_strings() -> None:
    """bool 字段上的 "true"/"false" 字符串是无损归一化，不是修复性容错。"""
    assert parse_json('{"approve": "false", "monologue": "x"}', _Card).approve is False
    assert parse_json('{"approve": "true", "monologue": "x"}', _Card).approve is True


@pytest.mark.parametrize(
    "text",
    [
        '{"approve": "yes", "monologue": "x"}',
        '{"approve": "False", "monologue": "x"}',
        '{"approve": "0", "monologue": "x"}',
        '{"approve": 0, "monologue": "x"}',
        '{"approve": 1, "monologue": "x"}',
    ],
)
def test_non_boolean_forms_still_rejected(text: str) -> None:
    """除 "true"/"false" 外的一切非布尔形式照旧严格拒收。"""
    with pytest.raises(JsonParseError, match="字段校验失败"):
        parse_json(text, _Card)


def test_str_field_keeps_boolean_spelled_strings_untouched() -> None:
    """归一化只作用于声明为 bool 的字段，str 字段的 "true" 原样保留。"""
    assert parse_json('{"approve": true, "monologue": "true"}', _Card).monologue == "true"


def test_optional_bool_field_normalizes_too() -> None:
    assert parse_json('{"approve": "false"}', _MaybeCard).approve is False
    assert parse_json('{"approve": null}', _MaybeCard).approve is None
    with pytest.raises(JsonParseError, match="字段校验失败"):
        parse_json('{"approve": "maybe"}', _MaybeCard)


def test_strict_integer_rejects_json_boolean() -> None:
    with pytest.raises(JsonParseError, match="字段校验失败"):
        parse_json('{"target_no": true}', _Target)


def test_parser_enforces_strict_types_even_if_model_forgets_config() -> None:
    with pytest.raises(JsonParseError, match="字段校验失败"):
        parse_json('{"target_no": true}', _OtherwiseLooseTarget)


def test_extra_field_is_rejected() -> None:
    with pytest.raises(JsonParseError, match="字段校验失败"):
        parse_json('{"approve": true, "monologue": "x", "phase": "wrong"}', _Card)


def test_missing_required_field_fails() -> None:
    with pytest.raises(JsonParseError, match="字段校验失败"):
        parse_json('{"approve": true}', _Card)
