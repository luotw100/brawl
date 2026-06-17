"""背景音乐 BGM：一条贯穿整场的低音量背景床。

固定顺序的循环播放列表——直播页（web/src/stage/bgm.ts 的 useBgm）与离线录制（record.py 的
bgm 床）共用这同一套顺序与音量。跨语言没法共享代码，两边必须手动保持一致——改这里就同步改
bgm.ts。

与音效（一拍一个 cue）不同，BGM 是连续床：按固定顺序连播、放完回到第一首。直播跟墙钟整场
不停（跨多局、幕间也铺着），录屏按视频时长循环铺满后截断——两边同序，所以录出来可复现。

音乐文件在 web/public/bgm/<name>.opus（构建后落到 web_dist/bgm/），走 Git LFS。名字即契约：
换曲只换同名 .opus，这张表不动。
"""

from __future__ import annotations

BGM_VOLUME = 0.16  # 背景床压在人声/音效之下（直播页 el.volume / mux 里 volume 滤镜同此值）

# 固定播放顺序：开场史诗 → 宫廷暗涌 → 王庭宴会 → 暗黑张力，放完循环回第一首。
BGM_PLAYLIST = [
    "crusade",
    "angevin",
    "master-of-the-feast",
    "volatile-reaction",
]
