"""从模型自由文本里稳健地抽出一个 JSON 对象并用 pydantic 校验。

为什么不用厂商结构化输出能力（response_format/json_schema、强制 tool_choice 之类）：各家
实现五花八门（思考型模型尤其挑），接一个新模型就可能踩 400。我们的 JSON 只有 2~3 个
扁平字段，纯靠 prompt 约束 + 这里的解析校验最通用——只要模型能吐文本就能用。

两道解析：先走严格路径（花括号配对扫描 + json.loads），合法输出原样通过、零改动；只有
严格失败才回退到 json_repair 救一次。回退专治实测见过的模型脏输出——用全角引号 ”/“ 当
字符串定界符、把闭合 } 写成 )、尾逗号、被截断少个右括号等——这些都是"模型本意是个对象、
只是手滑写脏了"，能救则救。真正抽不出对象（空输出、纯散文）仍抛 JsonParseError（fail-fast
不兜底：不会把空响应静默变成空对象）。
"""

from __future__ import annotations

import json

import json_repair
from pydantic import BaseModel, ValidationError


class JsonParseError(ValueError):
    """文本里抽不出合法 JSON 对象，或字段不满足模型约束。"""


def extract_json_object(text: str) -> str:
    """扒出文本中第一个完整的 {...}。

    按花括号配对扫描，并忽略字符串内的花括号/转义——因此能容忍：思考模型在 JSON 前面
    的大段推理、```json 代码围栏、以及 JSON 之后的尾巴文字。
    """
    start = text.find("{")
    if start == -1:
        raise JsonParseError(f"未找到 JSON 对象：{text[:120]!r}")
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise JsonParseError(f"花括号不配对：{text[:120]!r}")


def _extract_object(text: str) -> dict:
    """把文本解析成一个 JSON 对象（dict）：先严格、失败再 json_repair 救一次。

    抽不出对象（空输出/纯散文，救回的不是 dict）抛 JsonParseError——这是 fail-fast 的边界，
    绝不把"没有对象"静默兜底成空对象。"""
    try:
        data = json.loads(extract_json_object(text))
        if isinstance(data, dict):
            return data  # 严格路径成功：合法输出原样通过，不经 repair
    except (JsonParseError, json.JSONDecodeError):
        pass  # 落到下面的容错回退
    repaired = json_repair.repair_json(text, return_objects=True)
    if isinstance(repaired, dict):
        return repaired
    raise JsonParseError(f"未找到 JSON 对象：{text[:120]!r}")


def _clean_keys(data: dict) -> dict:
    """剥掉键名两端粘连的中文引号/空白（实测 deepseek-flash 偶发把键写成 `approve」`）。

    只清洗键、绝不动值；清洗后撞键视同脏输出，抛 JsonParseError。"""
    cleaned: dict = {}
    for k, v in data.items():
        ck = k.strip().strip("「」『』“”‘’\"'：:")
        if ck in cleaned:
            raise JsonParseError(f"键名清洗后冲突：{list(data)!r}")
        cleaned[ck] = v
    return cleaned


def parse_json[T: BaseModel](text: str, model: type[T]) -> T:
    """抽 JSON 对象（严格 + json_repair 回退）→ pydantic 校验（含类型矫正）。失败抛 JsonParseError。"""
    data = _clean_keys(_extract_object(text))
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise JsonParseError(f"字段校验失败：{e}") from e
