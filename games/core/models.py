"""Agent 模型规格。

现在只保留「逻辑模型名 → OpenCode 模型引用 + 展示信息」的映射。裸 LLM API 调用已移除，
所有推理都走 agent harness（当前实现为 OpenCode server）。
"""

from __future__ import annotations

from pydantic import BaseModel


class ModelSpec(BaseModel):
    """一个逻辑模型的展示与 OpenCode 映射规格。"""

    name: str
    display: str | None = None
    avatar: str | None = None
    # OpenCode server 上的模型引用，格式 providerID/modelID。
    # player=opencode 时优先按座位用它 mapping；为空则退到 --opencode-model 或 server 默认。
    opencode_model: str | None = None

