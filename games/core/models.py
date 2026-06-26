"""LLM 接入规格。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ModelSpec(BaseModel):
    """一个逻辑模型的 OpenAI 兼容接入规格。"""

    name: str
    base_url: str
    model_id: str
    api_key_env: str
    temperature: float = 0.9
    max_tokens: int | None = None
    timeout: float = 180
    extra_body: dict[str, Any] = Field(default_factory=dict)
    avatar: str | None = None
    display: str | None = None

