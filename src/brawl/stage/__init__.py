"""游戏盲演出壳：把存档（sentences.jsonl + meta.json）演成画面与视频。

本包不认任何玩法名词。玩法分三层交付能力（见 stagecast.StageGame）：
- stagecast：存档 → 舞台指令流（纯投影，语义唯一归宿在玩法包）
- protocol/plan：指令形状与节奏配方（玩法包自带，壳只按 kind 查表）
- reducer/summary/anchor：展示态折叠与录制挂钩
"""
