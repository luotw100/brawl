"""Qwen3-TTS 本地 runtime 小工具。

只在需要本地 TTS 时懒加载重依赖，避免默认环境必须安装 PyTorch/qwen-tts。
"""

from __future__ import annotations

import os
from typing import Any, Literal


def import_runtime(error: type[Exception]) -> tuple[Any, Any, Any]:
    try:
        import soundfile as sf
        import torch
        from qwen_tts import Qwen3TTSModel
    except ImportError as err:
        raise error(
            "缺少本地 Qwen3-TTS runtime；CPU 机器执行：cd games && uv sync --extra qwen3-tts-cpu；"
            "CUDA 机器执行：cd games && uv sync --extra qwen3-tts-cu128"
        ) from err
    return torch, sf, Qwen3TTSModel


def resolve_device(torch: Any, requested: Literal["auto", "cpu", "cuda"], threads: int, error: type[Exception]) -> tuple[str, str]:
    device = "cuda" if requested == "auto" and torch.cuda.is_available() else requested
    if device == "auto":
        device = "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise error("指定 --device cuda，但当前 PyTorch 环境不可用 CUDA")
    if device == "cpu":
        os.environ.setdefault("OMP_NUM_THREADS", str(threads))
        os.environ.setdefault("MKL_NUM_THREADS", str(threads))
        os.environ.setdefault("OPENBLAS_NUM_THREADS", str(threads))
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        return "cpu", "float32"
    return "cuda:0", "bfloat16"


def load_model(model_cls: Any, model: str, device_map: str, dtype: str) -> Any:
    return model_cls.from_pretrained(
        model,
        device_map=device_map,
        dtype=dtype,
        attn_implementation="eager",
    )
