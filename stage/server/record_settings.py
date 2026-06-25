"""RecordSettings：`stage-record` 的参数面（settings 即 CLI，flag 自动派生，绝不手写 argparse）。

节奏/画面旋钮（pace/speed/feed_window/prefetch_factor）继承自 EngineKnobs，与直播共享一份——
新旋钮只在基类加一次、两侧自动都有，不再两套配置手抄同步（见 settings.py 的 EngineKnobs）。

边界纪律：所有"合不合法"的校验在这里一次查清（replay 目录是真目录且含 events.jsonl、
fps/画布尺寸正；旋钮范围归 EngineKnobs）。下游（record.py）拿到的 settings 默认已合法，
字段已就绪，零校验分叉。

前端构建产物不在这里配：每次录制都现构建到临时目录（见 record.py 的 _build_web），
所以没有 --web-dist，也永远录不到旧 web——构建即新鲜，零地雷。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from server.settings import EngineKnobs

# VAAPI 硬件编码设备（N100 核显）。要用得当前用户在 render 组（节点是 crw-rw---- root:render）。
RENDER_DEVICE = "/dev/dri/renderD128"


class RecordSettings(EngineKnobs):
    """`stage-record <replay_dir>`：把一局 replay 离线录成视频文件。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_STAGE_RECORD_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    dir: CliPositionalArg[Path]  # replay 目录（含 events.jsonl + tts/）
    pace: float = 1.0  # 覆写 EngineKnobs 默认：录像跑足从容直播节奏（直播默认 0.5）
    width: int = 1920  # 录制画布宽：默认 16:9 横屏（舞台 layout 本就是横屏）；直播画面形状由 OBS/浏览器侧决定，不在此
    height: int = 1080  # 录制画布高
    fps: int = 30  # 输出帧率
    encoder: Literal["x264", "vaapi"] = "x264"  # 软件 H.264 veryfast（默认，开箱即用、跟得上实时）/ 硬件 VAAPI 单趟 HEVC（需 render 组，体积更小、上传平台友好）
    audio_shift: float = 0.0  # 音轨整体微调（秒，正=音频后移）：补开抓→首帧的常数延迟
    bgm: bool = True  # 叠加背景音乐床（--no-bgm 录纯人声/音效）
    limit_seconds: float | None = None  # 录像时长上限（秒）：到点即停的安全阀，也便于先出片段试看；缺省录到终局
    headed: bool = False  # 复用现有 $DISPLAY（带屏机本地可视调试）；默认起 Xvfb 虚拟屏（无头服务器照跑）

    @model_validator(mode="after")
    def _check(self) -> RecordSettings:
        # pace/speed/feed_window/prefetch 的范围校验在 EngineKnobs._check_knobs，这里只查录制专属。
        if self.fps <= 0:
            raise ValueError(f"fps 必须为正，收到 {self.fps}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"画布尺寸必须为正，收到 {self.width}×{self.height}")
        # 跨世界一致性：选了 VAAPI 就得真能访问核显设备，否则开工前就报清楚（别等 ffmpeg 深处崩）
        if self.encoder == "vaapi" and not os.access(RENDER_DEVICE, os.R_OK | os.W_OK):
            raise ValueError(f"VAAPI 设备不可访问：{RENDER_DEVICE}（把用户加进 render 组：sudo usermod -aG render $USER 后重登）；或用 --encoder x264")
        # 跨世界一致性：--headed 要复用现有屏，就必须真有 $DISPLAY（无头默认起 Xvfb，不需要）
        if self.headed and not os.environ.get("DISPLAY"):
            raise ValueError("--headed 需要现成的 $DISPLAY（带屏机才有）；无头服务器去掉 --headed，会自动起 Xvfb 虚拟屏")
        if not self.dir.is_dir():
            raise ValueError(f"replay 目录不存在：{self.dir}")
        if not (self.dir / "events.jsonl").is_file():
            raise ValueError(f"{self.dir} 不是 replay 目录（缺 events.jsonl）")
        return self

    @property
    def video_path(self) -> Path:
        """发布资源包约定：视频永远生成在 replay 目录里的固定文件名。"""
        return self.dir / "video.mp4"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,  # 直接 kwargs 最高（代码/测试照常生效）
            CliSettingsSource(
                settings_cls,
                cli_parse_args=cls._cli_args if cls._cli_args is not None else [],
                cli_prog_name="stage-record",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> RecordSettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None
