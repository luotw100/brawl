"""RecordSettings：`stage-record` 的参数面（settings 即 CLI，flag 自动派生，绝不手写 argparse）。

节奏/画面旋钮（pace/speed/feed_window/prefetch_factor）继承自 EngineKnobs，与直播共享一份——
新旋钮只在基类加一次、两侧自动都有，不再两套配置手抄同步（见 settings.py 的 EngineKnobs）。

边界纪律：所有"合不合法"的校验在这里一次查清（replay 目录是真目录且含 events.jsonl、
fps/画布尺寸正、web_dist 存在；旋钮范围归 EngineKnobs），并把可省字段（out / web_dist）的默认
在此解析成确定值，下游（record.py）拿到的 settings 默认已合法、字段已就绪，零校验分叉。
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

# 演出层前端构建产物：默认相对 server 包向上找 web/dist（与 docker 里 web_dist 同一份）。
_DEFAULT_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

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
    out: Path | None = None  # 输出 mp4；缺省落 <当前目录>/<replay 名>.mp4（在 _check 里解析）
    web_dist: Path | None = None  # 前端构建产物目录；缺省取 server 包旁的 web/dist
    pace: float = 1.0  # 覆写 EngineKnobs 默认：录像跑足从容直播节奏（直播默认 0.5）
    width: int = 1920  # 录制画布宽：默认 16:9 横屏（舞台 layout 本就是横屏）；直播画面形状由 OBS/浏览器侧决定，不在此
    height: int = 1080  # 录制画布高
    fps: int = 30  # 输出帧率
    encoder: Literal["x264", "vaapi"] = "x264"  # 软件 x264（默认，开箱即用）/ 硬件 VAAPI（需 render 组）
    audio_shift: float = 0.0  # 音轨整体微调（秒，正=音频后移）：补 WS→渲染的常数延迟
    bgm: bool = True  # 叠加背景音乐床（--no-bgm 录纯人声/音效）
    limit_seconds: float | None = None  # 录像时长上限（秒）：到点即停的安全阀，也便于先出片段试看；缺省录到终局
    headed: bool = False  # 调试用：起带头浏览器（默认无头）

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
        if not self.dir.is_dir():
            raise ValueError(f"replay 目录不存在：{self.dir}")
        if not (self.dir / "events.jsonl").is_file():
            raise ValueError(f"{self.dir} 不是 replay 目录（缺 events.jsonl）")
        # 解析可省字段的默认值：校验只查不改，填默认是另一回事，这里一次性补齐给下游确定值
        if self.web_dist is None:
            self.web_dist = _DEFAULT_WEB_DIST
        if not self.web_dist.is_dir():
            raise ValueError(f"web_dist 不存在（前端未构建？）：{self.web_dist}")
        if self.out is None:
            self.out = Path.cwd() / f"{self.dir.name}.mp4"
        return self

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
