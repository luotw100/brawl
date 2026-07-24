"""RecordSettings：`brawl record` 的参数面（settings 即 CLI，flag 自动派生，绝不手写 argparse）。

录制只支持一种成品规格（1920×1080 @ 30fps，含 BGM），画布/帧率/音画同步都是代码常数——
它们不是用户旋钮：音画对齐由首帧门闩（页面 paint → 开抓 → 确认首帧 → 释放播放时钟）兜底，
对话区窗口与音频预热是引擎内部常数（见 engine.py）。

边界纪律：所有"合不合法"的校验在这里一次查清（replay 目录是真目录且含存档两件套
（sentences.jsonl + meta.json）、编码器设备可达）。下游（record.py）拿到的 settings
默认已合法，字段已就绪，零校验分叉。

前端构建产物不在这里配：每次录制都现构建到临时目录（见 record.py 的 _build_web），
所以没有 --web-dist，也永远录不到旧 web——构建即新鲜，零地雷。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, model_validator
from pydantic_settings import CliPositionalArg, SettingsConfigDict

from brawl.runtime.cli_settings import CliSettings

# VAAPI 硬件编码设备（N100 核显）。要用得当前用户在 render 组（节点是 crw-rw---- root:render）。
RENDER_DEVICE = "/dev/dri/renderD128"

# 成品规格定死：16:9 横屏 1080p30（舞台 layout 本就是横屏）。要新规格就改代码，不开旋钮。
WIDTH = 1920
HEIGHT = 1080
FPS = 30


class RecordSettings(CliSettings):
    """把一局 replay 存档离线录成 video.mp4（1920×1080 · 30fps · 含 BGM）。"""

    CLI_PROG: ClassVar[str] = "brawl record"
    model_config = SettingsConfigDict(
        env_prefix="BRAWL_RECORD_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    dir: CliPositionalArg[Path] = Field(description="replay 目录（含 sentences.jsonl + meta.json + tts/）")
    speed: float = Field(default=1.0, description="快录倍速：4 倍速则 40 分钟的局约 10 分钟录完")
    encoder: Literal["x264", "vaapi"] = Field(
        default="vaapi",
        description="编码器：vaapi=核显硬编（体积小，需 render 组）；x264=软编兜底",
    )
    limit_seconds: float | None = Field(default=None, description="只录前 N 秒，先试看片段用；缺省录到终局")

    @model_validator(mode="after")
    def _check(self) -> RecordSettings:
        if not 0 < self.speed <= 16:
            raise ValueError(f"--speed 必须在 (0, 16]，收到 {self.speed}（页面 playbackRate 上限 16）")
        if self.limit_seconds is not None and self.limit_seconds <= 0:
            raise ValueError(f"--limit-seconds 必须为正，收到 {self.limit_seconds}")
        # 跨世界一致性：选了 VAAPI 就得真能访问核显设备，否则开工前就报清楚（别等 ffmpeg 深处崩）
        if self.encoder == "vaapi" and not os.access(RENDER_DEVICE, os.R_OK | os.W_OK):
            raise ValueError(f"VAAPI 设备不可访问：{RENDER_DEVICE}（把用户加进 render 组：sudo usermod -aG render $USER 后重登）；或用 --encoder x264")
        if not self.dir.is_dir():
            raise ValueError(f"replay 目录不存在：{self.dir}")
        missing = [name for name in ("sentences.jsonl", "meta.json") if not (self.dir / name).is_file()]
        if missing:
            raise ValueError(f"{self.dir} 不是 replay 存档目录（缺 {', '.join(missing)}）")
        return self

    @property
    def video_path(self) -> Path:
        """发布资源包约定：视频永远生成在 replay 目录里的固定文件名。"""
        return self.dir / "video.mp4"
