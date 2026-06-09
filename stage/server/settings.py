"""StageSettings：演出服务的配置唯一真源（settings 即 CLI/env，自动派生，绝不手写 argparse）。

边界纪律：所有"合不合法"的校验在这里一次查清（目录存在、token 非空、pace 范围），
下游（engine / app）拿到的 settings 默认已合法，零校验分叉。
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class StageSettings(BaseSettings):
    """`BRAWL_STAGE_*` 环境变量即配置面。无默认的字段必须显式给出，缺失即报错。"""

    model_config = SettingsConfigDict(env_prefix="BRAWL_STAGE_", extra="ignore")

    replay_root: Path  # replay 目录们的根：每个子目录一局（含 events.jsonl）；显示名/音色等身份信息都在各局 meta.json 自带，stage 不再读 games 的 cast.yaml
    stage_token: str  # 观众画面 /ws 的准入 token（必填，无默认——舞台也不许裸奔）
    console_token: str  # 导播台/控制 API 的 token（必填，无默认——公网暴露不许裸奔）

    host: str = "0.0.0.0"
    port: int = 8000
    web_dist: Path | None = None  # 前端构建产物目录；None = 不托管静态（开发时走 Vite）

    # 演出节奏初值（运行期可由导播台改，engine 持有活值）
    pace: float = 0.5  # 0~1：0 瞬间倾倒，1 慢慢朗读
    intermission_seconds: float = 20.0  # 局间幕间页停留
    loop_pool: bool = True  # 队列耗尽时自动从 replay_root 随机补局，保证永不黑屏
    feed_window: int = 2  # 对话区显示最近几拍（1~3；手机屏被平台 UI 挤占，当前发言为主角）
    prefetch_factor: int = 2  # 提前几拍把待播音频 URL 露给页面预热（像 dataloader 的 prefetch_factor）

    @model_validator(mode="after")
    def _check(self) -> StageSettings:
        if not 0.0 <= self.pace <= 1.0:
            raise ValueError(f"pace 必须在 0~1 之间，收到 {self.pace}")
        if not 1 <= self.feed_window <= 3:
            raise ValueError(f"feed_window 必须在 1~3 之间，收到 {self.feed_window}")
        if self.prefetch_factor < 0:
            raise ValueError(f"prefetch_factor 不能为负，收到 {self.prefetch_factor}")
        if not self.replay_root.is_dir():
            raise ValueError(f"replay_root 不存在：{self.replay_root}")
        if not self.stage_token:
            raise ValueError("stage_token 不能为空")
        if not self.console_token:
            raise ValueError("console_token 不能为空")
        if self.web_dist is not None and not self.web_dist.is_dir():
            raise ValueError(f"web_dist 不存在：{self.web_dist}")
        return self
