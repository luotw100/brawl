"""core.parsing 的单一严格路径：完整 JSON object 入，任何修复或夹带都拒绝。"""

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


def test_strict_boolean_rejects_string() -> None:
    with pytest.raises(JsonParseError, match="字段校验失败"):
        parse_json('{"approve": "false", "monologue": "x"}', _Card)


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
