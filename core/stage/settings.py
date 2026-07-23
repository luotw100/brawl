"""EngineKnobs：引擎节奏/画面旋钮的唯一真源（录制场景的配置基类）。

收分叉：这些旋钮原本在直播/录制两套 settings 里各写一遍——抽成共享基类后，新旋钮只在
这里加一次、校验也只写一遍；record 起引擎时按本基类字段名机械转移，不再逐字段手抄。
"""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings


class EngineKnobs(BaseSettings):
    """引擎节奏/画面旋钮。

    默认取值可被子类按场景覆写——旋钮的"含义+校验"在此共享，
    "默认取值"是各场景的合理差异，不算分叉。"""

    speed: float = 1.0  # 时间轴倍速：每拍时长（含音频地板）÷speed → 引擎少 sleep、原生快录/快播；音频按 speed 提速贴合压缩后的拍。
    # 上限 16：页面把 item.speed 直接赋给 HTMLMediaElement.playbackRate，Chrome 只支持 ≤16，
    # 超界抛 NotSupportedError 会打断整页渲染（首帧门随之等死）。ffmpeg atempo 侧无此限。
    feed_window: int = 2  # 对话区显示最近几拍（1~3；当前发言为主角）
    prefetch_factor: int = 2  # 提前几拍把待播音频 URL 露给页面预热（像 dataloader 的 prefetch_factor）

    @model_validator(mode="after")
    def _check_knobs(self) -> EngineKnobs:
        if not 0 < self.speed <= 16:
            raise ValueError(f"speed 必须在 (0, 16]，收到 {self.speed}（页面 playbackRate 上限 16）")
        if not 1 <= self.feed_window <= 3:
            raise ValueError(f"feed_window 必须在 1~3 之间，收到 {self.feed_window}")
        if self.prefetch_factor < 0:
            raise ValueError(f"prefetch_factor 不能为负，收到 {self.prefetch_factor}")
        return self
