---
name: repository-overview
description: brawl 仓库架构全景。AI 直播对战平台项目背景、当前 Avalon 玩法、跨玩法运行时框架、目录指路、核心不变量、阅读顺序。
---

# brawl 仓库架构

这是一张**指向代码的地图**，不是代码镜像。具体字段/参数以源文件为准。改文档前先比对代码。

## 项目背景

`brawl` 是一个 AI 直播对战平台：让若干常驻人设 AI 互相玩社交推理类游戏，目标是 7x24 无人值守直播。观众全程上帝视角，靠将来的押注/积分机制参与互动。

`games` 是**对局快照生成器**，不是直播实时引擎：CLI 离线生成一局 replay（事件时间线），直播时由 `stage` 回放这份 replay，不实时跑模型。生成器里没有 wall-clock 节奏控制。

当前保留的玩法是 **Avalon**。谁是卧底已下线删除；新增 `core/` 承载跨玩法运行时，方便后续接狼人杀。

## 仓库布局

```
brawl/
├── games/
│   ├── core/              # 跨玩法运行时：actor/registry/setup/threads/host/recorder/resume/runner/parsing/events/models
│   ├── avalon/                  # Avalon 玩法：规则、状态机、提示词、host、replay/tts、测试、data
│   ├── cli.py                   # 子命令路由，只分发，不认识玩法参数
│   ├── llmview/                 # llm_calls 查看工具
│   └── voicelab/                # 音色铸造工具
├── stage/                       # replay → 直播画面/导播台
└── live/                        # 未做：直播平台接入
```

## games 分层

- `core.models.ModelSpec`：模型接入规格。
- `core.registry`：模型/人设/流派/主持人注册表与 yaml 加载。
- `core.setup`：通用边界解析，校验 cast/strategies/models/host/extractor/replay/env。
- `core.actor`：`ModelBackend` / `LiveBackend` / `LLMActor`，负责模型调用、重试、extractor JSON 解析与 `llm_calls` 审计。
- `core.threads` / `host`：每席位私有线程、公开广播、主持人口播事件。
- `core.recorder`：`DirRecorder` / `NullRecorder`，负责 replay 三件套与 avatars。
- `core.resume`：checkpoint 数据库路径、sidecar 水位和续跑截断点。
- `core.runner`：LangGraph `custom/values/checkpoints` 驱动，事件扇出和 checkpoint 水位对齐。
- `core.parsing` / `events`：稳健 JSON 抽取、`decision_id` 与 `emit`。

Avalon 自己保留玩法专属内容：

- `rules.py`：规则常数、队伍人数、失败门槛、夜晚可见性。
- `state.py`：Avalon 状态、角色、公开账本、结构化决策结果。
- `validation.py`：配置 + registry + rng → 确定 `GameSetup`，开局随机和跨引用校验都在这里完成。
- `nodes.py` / `graph.py`：裁判节点与 LangGraph 状态机。
- `registry.py` / `host.py`：Avalon 数据目录和 Host 的薄门面，真实实现来自 `core`。
- `player.py`：Avalon 的 `PlayerView`、`FakeBackend`、八个决策动作；通用 LLM 链路来自 `core.actor`。
- `prompts.py` / `host.py`：提示词与主持人串场。
- `run.py`：装配 Avalon 的 setup/state/deps，并调用 `core.runner` 跑图。

## 核心不变量

1. 私密信息只进该席位 prompt 和观众事件流，绝不进 AI 之间可见的公开记录。Avalon 的公开 `ProposalRecord` 只含提案、明牌表决、任务破坏票张数，不含谁出了破坏票。
2. 内心独白只给观众，绝不进别人上下文。
3. 开局随机前置到 `validation.resolve_and_validate`，图里拿到确定的 `GameSetup`。Avalon 对局过程没有裁判侧随机。
4. 裁判节点纯代码：AI 只决定说什么/选谁/投什么，不决定流程推进、计票、胜负。
5. 可替换外部依赖走 Deps。`--player llm|random` 只替换底层 `ModelBackend`，状态机不分叉。
6. replay 是完整快照：`events.jsonl`、`llm_calls.jsonl`、`meta.json`，目录存在即拒绝覆盖。不给 `--replay` 走 `NullRecorder`。
7. Stage 的 `engine/hub/overlay` 游戏无关；具体玩法通过 adapter 把 replay 事件翻成舞台指令。当前 `stage/server/scene.py` 是 Avalon adapter。

## 跑起来

```bash
cd games && uv sync
uv run brawl avalon --player random --players 10 --percival --morgana --mordred --oberon --lady --replay /tmp/avalon1
uv run brawl avalon-tts /tmp/avalon1
uv run pytest -q
```

## 阅读顺序

1. `games/core/`：先看跨玩法运行时边界。
2. `games/avalon/graph.py` → `nodes.py`：一局怎么走。
3. `games/avalon/state.py`：状态与信息隔离落点。
4. `games/avalon/player.py` + `prompts.py`：玩家视图、fake 后端、决策提示词。
5. `games/avalon/validation.py` + `settings.py` + `registry.py`：配置、开局摇定、数据加载。
6. `games/avalon/tests/`：不变量如何被测试守住。
