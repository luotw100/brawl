"""音效共享常量。

一拍 → 该放哪些音效（名字 + 拍内偏移秒）的映射由 `server/plans/<game>.yaml` 描述，
通用 playplan compiler 产出 sfx tracks。直播页和离线录制都消费同一份 tracks，不再有两套 cue 表。

音效文件在 web/public/sfx/<name>.opus（构建后落到 web_dist/sfx/）。名字即契约：换素材只换
同名文件，映射不动。本模块只放两侧都要的统一音量。
"""

from __future__ import annotations

SFX_VOLUME = 0.7  # 音效统一压在人声之下（直播页 el.volume / mux 里 volume 滤镜同此值）
