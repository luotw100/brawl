# brawl

AI 对战游戏：让若干常驻人设 AI 互相玩社交推理游戏（阿瓦隆、狼人杀），把对局录成视频发布。
观众全程上帝视角（看得见所有底牌与内心独白），这是节目的核心看点。

**根本纪律：brawl 就是一个完整的游戏，replay 是游戏存档。** 生成（AI 对局）、回放（终端）、
配音（TTS）、演出（录屏出片）都是同一个代码库对同一份存档的不同消费——"懂规则"只发生在
玩法包里，演出是把存档重放一遍并逐步投影成舞台指令。

## 顶层结构

单一 uv 项目、单一 Python 包（src 布局）、一条 CLI：

- `src/brawl/runtime/` —— 对局运行时（Sentence Runtime）：驱动循环 / opencode 调用 / 句子日志 / resume / TTS / 终端回放。游戏盲。
- `src/brawl/stage/` —— 演出壳：单局引擎 / playplan 节奏编译 / 录屏 / 封面 / 一次性 WS 服务。游戏盲。
- `src/brawl/games/avalon/` `src/brawl/games/werewolf/` —— 玩法包：规则（rules_engine）+ 演出（stagecast / protocol / plan.yaml），一个玩法一个文件夹，自包含。
- `src/brawl/cli.py` —— 组合根：子命令路由 + 玩法能力登记（板子/演出/resume；runtime/stage 不 import 玩法包）。
- `src/brawl/games/run.py` —— `brawl run`/`brawl resume`：跨玩法统一参数面 + 板子注册表。
- `src/brawl/voicelab/` —— 音色铸造工具。
- `web/` —— 舞台前端（React/Vite）：录屏与（未来的）直播共用同一页。

## 出片流水线

```bash
uv sync && (cd web && npm install)

uv run brawl run --board merlin-assassin-percival-morgana-mordred-oberon-lady-10 --replay /tmp/avalon1  # ① 生成存档（AI 对局）
uv run brawl tts /tmp/avalon1                       # ② 配音（阿里云 DashScope，增量）
uv run brawl record /tmp/avalon1                    # ③ 出片 → /tmp/avalon1/video.mp4
uv run brawl cover /tmp/avalon1                     # ④ 封面 → /tmp/avalon1/covers/
```

开局侧：`brawl run --board <板子>` 一个参数面打所有玩法——板子决定玩法、人数与角色构成
（当前：阿瓦隆 10 人全角色带湖仙 / 狼人杀 12 人预女猎白），其余参数（`--backend`/`--seed`/
`--cast`/`--models` 等）全板通用。`--backend random` 零网络冒烟；默认 `--backend opencode`
走托管的 OpenCode（每座位独立 server + 隔离 workspace，需 `opencode auth login` 与 `bwrap`）。
存档目录即发布单元：`sentences.jsonl`（句子日志=checkpoint）、`meta.json`（冻结配置）、
`tts/`、`avatars/`、`video.mp4`、`covers/`。续跑只认 `brawl resume --replay <dir>`，
对局参数从 meta.json 恢复，不能附加其他参数。

其他命令：`brawl console <dir>`（终端回放存档，零网络）；`brawl voice-forge`（音色铸造，
见 voicelab）。

录屏：自起一次性播放栈（单局引擎 + uvicorn 到 127.0.0.1 随机口）+ Xvfb 虚拟屏 + 带头
Chromium + ffmpeg 单趟硬编，边播边录，音轨离线按拍拼接。成品规格固定 1920×1080 @ 30fps
含 BGM；`--limit-seconds` 先出片段试看，`--speed` 快录（上限 16）。覆盖已有 video.mp4 /
封面时，需输入当场生成的 4 位随机冷静码确认。

## 加玩法

一个文件夹一个玩法：rules（Rules 协议）+ stagecast（StageGame bundle）+ plan.yaml +
前端渲染器 + tests。runtime/stage 两个包都不 import 玩法；CLI 登记即接入全部命令，
测试从 `src/brawl` 自动发现、无需登记。

## 测试与检查

```bash
uv run pytest -q
uv run ruff check .
```

## 直播

本期只做录屏；7×24 频道（导播台/片库/overlay/公网边缘）已从代码库移除（在 git 历史里），
回归时在单局引擎之上重建调度层。
