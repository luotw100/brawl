"""音效共享常量。

一拍 → 该放哪些音效（名字 + 拍内偏移秒）的映射是**玩法专属**的，在各玩法插件的 `cues`
（如 games/avalon/plugin.py 的 AvalonPlugin.cues）里；直播页（web/src/stage/sfx.ts 的 cuesFor）
与离线录制（record.py 的 mux）共用同一套语义，跨语言没法共享代码，所以两边手动保持一致。

音效文件在 web/public/sfx/<name>.opus（构建后落到 web_dist/sfx/）。名字即契约：换素材只换
同名文件，映射不动。本模块只放两侧都要的统一音量。
"""

from __future__ import annotations

SFX_VOLUME = 0.7  # 音效统一压在人声之下（直播页 el.volume / mux 里 volume 滤镜同此值）
