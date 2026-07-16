---
name: repository-overview
description: brawl 仓库架构全景。AI 直播对战平台项目背景、Avalon/狼人杀玩法、Sentence Runtime 跨玩法运行时、目录指路、核心不变量、阅读顺序。
---

# brawl 仓库架构

这是一张**指向代码的地图**，不是代码镜像。具体字段/参数以源文件为准。改文档前先比对代码。

## 项目背景

`brawl` 是一个 AI 直播对战平台：让若干常驻人设 AI 互相玩社交推理类游戏，目标是 7x24 无人值守直播。观众全程上帝视角，靠将来的押注/积分机制参与互动。

`games` 是**对局快照生成器**，不是直播实时引擎：CLI 离线生成一局 replay（句子时间线），直播时由 `stage` 回放这份 replay，不实时跑模型。生成器里没有 wall-clock 节奏控制。

当前玩法：**Avalon**（5~10 人）与**狼人杀**（12 人预女猎白）。运行时是新基建 **Sentence Runtime**（`core/sr/`）：LangGraph 已整体移除，设计文档见 `design/sentence-runtime.md`。

## 仓库布局

```
brawl/
├── games/
│   ├── core/             # 跨玩法运行时
│   │   ├── sr/           #   Sentence Runtime：types/rules/loop/journal/actor/opencode_backend/console/tts/messages/dummy
│   │   └── …             #   registry/setup/threads/parsing/models/opencode_runtime
│   ├── avalon/           # Avalon 玩法包：rules_engine/rules/state/view/prompts/validation/settings/run/data/tests
│   ├── werewolf/         # 狼人杀玩法包：同构
│   ├── cli.py            # 子命令路由（avalon/werewolf/console/tts/voice-forge/chat-view/slim-audio），只分发
│   ├── llmview/          # chat-view：旧格式 llm_calls 查看工具
│   └── voicelab/         # 音色铸造工具
├── stage/                # replay → 直播画面/导播台（消费侧待切到 sentences 格式）
└── live/                 # 未做：直播平台接入
```

## games 分层

- `core.sr.types`：游戏盲类型——`Delivery`（一段话 + channels 路由）/ `Sentence`（日志原子单位）/ `Expectation` / `ParseResult` / `HostEmit`。channel = 席位号 | broadcast | audience。
- `core.sr.rules`：玩法插件协议（Protocol）。`initial_state/expected/view/parse/apply/advance/pending/turn_id/pack/unpack`。语义唯一落脚点。
- `core.sr.loop`：驱动循环。并发收齐同时行动的座位，speak+parse 同单元重试（坏 JSON/非法目标重调 actor），按座位序确定性落日志/apply；resume = snapshot（可选）+ 重放 sentences。host 串场走 `rules.pending` 纯函数，loop 不对 host 调 actor。
- `core.sr.journal`：replay 三件套——`sentences.jsonl`（句子日志即 checkpoint）、`meta.json`、`_snapshot.json`（加速快照，非正确性承重墙）+ avatars。不给 `--replay` 走 NullJournal。
- `core.sr.actor` / `opencode_backend`：`OpenCodeActor`（每座位一个 opencode session，失败轮自动清理）、`RandomActor`（零网络 sampler，由玩法 rules.random_action 供料）、`OpenCodeBackend`（HTTP 协议、逐座位模型映射）。
- `core.sr.console` / `tts`：`brawl console` / `brawl tts`，跨玩法，只消费 sentences.jsonl + meta.json。
- `core.registry` / `setup`：模型/人设/流派/主持人注册表与边界解析（人设唯一/可重复双模式）。
- `core.threads` / `parsing` / `models` / `opencode_runtime`：席位线程、稳健 JSON 抽取、模型规格、opencode 托管生命周期（bwrap 沙盒、固定版本 1.17.20）。

玩法包（`avalon/`、`werewolf/` 同构）：

- `rules_engine.py`：玩法全部语义——阶段机、计票/判负、信息隔离、prompt 构造、JSON 解析校验。
- `rules.py`：规则表（阵营/角色/板子/纯函数）。
- `state.py` / `view.py` / `prompts.py`：状态与结构化决策、actor 视图、全部文案。
- `validation.py` / `settings.py` / `registry.py` / `run.py` / `data/` / `tests/`：配置边界、CLI 参数面、数据加载、入口装配、各自数据与测试。

## 核心不变量

1. **core 搜不到任何玩法名词**。core 不解析句子内容；路由（谁听得到）由 rules 在 parse 里用 channels 声明，信息隔离压在纯代码拓扑上，不压在模型行为上。
2. 私密信息只进该席位 view 和观众流（channels 含 audience 的 Delivery），绝不进非授权席位 inbox；内心独白（monologue）只给观众。
3. prompt 文本由 rules.view 生成，core 原样发给 actor，不套通用模板。
4. 开局随机前置到 `validation.resolve_and_validate`；对局过程没有裁判侧随机（回放确定性：重放 apply 必须与实时一致）。
5. 裁判全在 rules：AI 只决定说什么/选谁/投什么，不决定流程推进、计票、胜负。非法产出 parse 抛 ValueError → loop 重试，绝不静默上桌。
6. replay 是完整快照：`sentences.jsonl`、`meta.json`、`_snapshot.json`，目录存在即拒绝覆盖。模型调用审计保留在 OpenCode 会话库（`<replay>/_opencode/storage/opencode.db`），本地不再落 llm_calls.jsonl。
7. Stage 的 `engine/hub/overlay` 游戏无关；stage 消费侧当前仍读旧 events.jsonl，切到 audience 句子流是设计文档阶段 6 的待办。

## 跑起来

```bash
cd games && uv sync
uv run brawl avalon --player random --players 10 --percival --morgana --mordred --oberon --replay /tmp/avalon1
uv run brawl werewolf --player random --replay /tmp/ww1
uv run brawl console /tmp/avalon1
uv run pytest -q
```

## 阅读顺序

1. `design/sentence-runtime.md`：运行时设计（为什么丢 LangGraph、边界红线、resume 模型）。
2. `games/core/sr/types.py` → `rules.py` → `loop.py` → `journal.py`：跨玩法运行时边界。
3. `games/avalon/rules_engine.py` 或 `games/werewolf/rules_engine.py`：一个完整 Rules 实现长什么样。
4. 玩法的 `prompts.py` + `view.py`：prompt 构造与信息隔离落点。
5. 玩法的 `validation.py` + `settings.py` + `registry.py`：配置、开局摇定、数据加载。
6. 玩法的 `tests/`：不变量如何被测试守住。

