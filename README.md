# brawl

AI 直播对战平台：让若干常驻人设 AI 互相玩社交推理游戏（谁是卧底、狼人杀、辩论……），目标 7×24 无人值守直播。观众上帝视角，靠押注与积分榜互动。

**当前进度**：第一个玩法「谁是卧底」的纯游戏逻辑层已实现、可跑、有测试。画面与直播互动尚未开始。

## 顶层结构

按关注点切分的组件 monorepo，组件间靠结构化事件流解耦：

- `games/` —— 【已实现】纯游戏逻辑，一个独立 uv 项目；一个文件夹一个玩法，自包含。
- `stage/` —— 【未做】画面渲染：立绘 / 字幕 / TTS / OBS overlay。
- `live/` —— 【未做】直播平台接入：抖音 API / 弹幕 / 观众押注。

## 快速开始

```bash
cd games && uv sync
uv run brawl undercover --player random \
  --timers-deal 0 --timers-opening-bet 0 --timers-round-bet 0 --timers-elim 0 --timers-postgame 0
```

## 架构地图

完整背景、技术架构、目录指路、核心不变量、阅读顺序见
[.agents/skills/repository-overview/SKILL.md](.agents/skills/repository-overview/SKILL.md)。
