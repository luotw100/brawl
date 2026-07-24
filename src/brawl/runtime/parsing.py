"""模型动作 JSON 的严格解析边界。

一次回答必须恰好是一个 JSON object；除首尾空白外，不允许推理文字、代码围栏、第二个
object 或任何修复性容错。字段类型、必填项与额外字段由调用方传入的严格 Pydantic action
model 声明。解析或校验失败统一抛 ``JsonParseError``，交给 runtime 重试。

``action_schema`` 是同一份 action model 的另一面：玩法把它挂到 view 上，OpenCode
后端据此请求 StructuredOutput——服务端按 schema 强约束形状，parse 端严格校验语义，
生成与解析共享同一份 model 定义，永不漂移。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError


class JsonParseError(ValueError):
    """回答不是唯一合法 JSON object，或字段不满足 action schema。"""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝含重复键的歧义对象（``json.loads`` 默认会静默保留最后一个值）。"""
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise JsonParseError(f"JSON 对象包含重复键：{key}")
        data[key] = value
    return data


def _reject_nonfinite(value: str) -> object:
    """Python JSON 解码器默认接受 NaN/Infinity；动作协议只接受标准 JSON。"""
    raise JsonParseError(f"JSON 包含非标准数值：{value}")


def _load_object(text: str) -> dict:
    """严格读取完整回答；``json.loads`` 只额外容许 JSON 两端的空白。"""
    try:
        data = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise JsonParseError(f"回答必须恰好是一个合法 JSON 对象：{exc.msg}") from exc
    if not isinstance(data, dict):
        raise JsonParseError(f"回答必须是 JSON 对象，实际为 {type(data).__name__}")
    return data


def parse_json[T: BaseModel](text: str, model: type[T]) -> T:
    """完整 JSON object → action model；任何失败都转换为 ``JsonParseError``。"""
    data = _load_object(text)
    try:
        return model.model_validate(data, strict=True)
    except ValidationError as exc:
        raise JsonParseError(f"字段校验失败：{exc}") from exc


def action_schema(model: type[BaseModel], phase: str) -> dict[str, Any]:
    """action model → StructuredOutput JSON Schema。

    `phase` 字段收窄为当前阶段常量：模型不可能填出串号的阶段值，直接消灭一类
    重试。其余字段形状与 rules.parse 的严格校验共享同一份 model 定义。
    """
    schema = model.model_json_schema()
    schema["properties"]["phase"] = {"const": phase}
    return schema
