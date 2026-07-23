---
name: repository-overview
description: brawl 仓库架构全景。AI 对战游戏项目背景、Avalon/狼人杀玩法、Sentence Runtime 对局运行时、stagecast 演出投影、目录指路、核心不变量、阅读顺序。
---

# brawl 仓库架构

这是一张**指向代码的地图**，不是代码镜像。具体字段/参数以源文件为准。改文档前先比对代码。

## 项目背景

`brawl` 是一个 AI 对战游戏：让若干常驻人设 AI 互相玩社交推理类游戏，把对局录成视频发布。
观众全程上帝视角（看得见所有底牌与内心独白）。

**根本纪律：brawl 就是一个完整的游戏，replay 是游戏存档。** 生成、终端回放、TTS、录屏都是
同一代码库对同一存档的消费。曾有的 games/stage 两套系统（靠 events.jsonl 契约传递信息、
两边分别理解规则）已随 One Brawl 改造整体拆除（设计文档见 git 历史）。

当前玩法：**Avalon**（5~10 人）与**狼人杀**（12 人预女猎白）。

## 仓库布局

```
brawl/
├── src/brawl/
│   ├── runtime/          # 对局运行时：types/rules/loop/journal/actor/
│   │                     #   opencode_backend/console/tts/files/messages +
│   │                     #   registry/setup/threads/parsing/models/opencode_runtime + tests
│   ├── stage/            # 演出壳：protocol(信封基类)/stagecast(契约+registry)/media/
│   │                     #   engine(单局播放头)/playplan/server/record/cover/bgm/sfx/hub + tests
│   ├── games/
│   │   ├── avalon/       # 玩法包：rules_engine/rules/state/view/prompts/validation/settings/run
│   │   │                 #   + stagecast(演出投影)/protocol(指令形状)/plan.yaml(节奏)/data/tests
│   │   └── werewolf/     # 同构
│   ├── voicelab/         # 音色铸造工具
│   ├── cli.py            # 组合根：子命令路由 + StageGame 登记（runtime/stage 不 import 玩法包）
│   └── paths.py          # REPO_ROOT：.env / web/ 等仓库级资源的定位锚点
└── web/                  # 舞台前端（React/Vite）：stage/ 渲染页 + games/<game>.ts 协议类型
```

## 分层

**生成路径**（brawl.runtime + 玩法 rules）：

- `brawl.runtime.types`：游戏盲类型——`Delivery`（一段话 + channels 路由）/ `Sentence`（日志原子单位）/ `Expectation` / `ParseResult` / `HostEmit`。channel = 席位号 | broadcast | audience。
- `brawl.runtime.rules`：玩法插件协议（Protocol）。`initial_state/expected/view/parse/apply/advance/pending/turn_id/pack/unpack`。语义唯一落脚点。
- `brawl.runtime.loop`：驱动循环。并发收齐同时行动的座位，speak+parse 同单元重试，按座位序确定性落日志/apply；resume = 从头严格重放。host 串场走 `rules.pending` 纯函数。
- `brawl.runtime.journal`：replay 三件套——`sentences.jsonl`（句子日志即 checkpoint）、`meta.json`、`_snapshot.json`（加速快照）+ avatars。
- `brawl.runtime.actor` / `opencode_backend` / `opencode_runtime`：OpenCode 托管（每座位独立 server + bwrap 隔离）、RandomActor（零网络 sampler）。
- `brawl.runtime.console` / `tts`：`brawl console` / `brawl tts`，跨玩法，只消费 sentences.jsonl + meta.json。

**演出路径**（brawl.stage + 玩法 stagecast）：

- `games/<game>/stagecast.py`：演出投影——存档（sentences+meta）经 MediaResolver 重放成指令流。
  规则事实只取自 action payload（生成时冻结的 god-view 真相），绝不重新计票、绝不反解
  turn_id；展示上下文（quest/attempt/day、当轮独白）随流累积。同文件交付 StageGame
  bundle（reducer/summary/is_opening_anchor/terminal/plan_path）。
- `brawl.stage.stagecast`：Stagecast/MediaResolver 契约 + StageGame + registry（按 meta.game 派发）。
- `brawl.stage.engine`：单局播放头——stagecast → playplan 编译 → 逐拍 emit（折 Scene、
  publish 全量状态、按时长停留）→ 演完返回。频道调度（直播回归时）是它之上的另一层。
- `brawl.stage.playplan`：plan YAML → 确定性 StageBeat/AudioTrack（节奏/主持贴靠/音效 cue）。
- `brawl.stage.record` / `cover`：`brawl record`（Xvfb+Chromium+ffmpeg 单趟硬编，音轨按拍
  离线拼接）与 `brawl cover`。record 内部一次性服务见 `brawl.stage.server`（127.0.0.1，无 token）。

**玩法包**（games/avalon/ games/werewolf/ 同构）：`rules_engine.py`（玩法全部语义）、`rules.py`（规则表）、
`state.py`/`view.py`/`prompts.py`、`validation.py`/`settings.py`/`registry.py`/`run.py`、
`stagecast.py`/`protocol.py`/`plan.yaml`、`data/`、`tests/`。

## 核心不变量

1. **runtime/stage 搜不到任何玩法名词**；玩法分发经 registry，装配只在 brawl/cli.py。
2. **"懂规则"只发生在玩法包**。聚合（票型/任务牌/狼刀结果）只能出自 rules 的 state 或
   action payload；stagecast 不得重新计数，不得反解 turn_id。
3. 路由（谁听得到）由 rules 在 parse 里用 channels 声明；私密信息只进授权席位 inbox 和
   观众流；内心独白只给观众。
4. prompt 文本由 rules.view 生成，core 原样发给 actor，不套通用模板。
5. 开局随机前置到 `validation.resolve_and_validate`；对局过程没有裁判侧随机（回放确定性）。
6. 裁判全在 rules：非法产出 parse 抛 ValueError → loop 重试，绝不静默上桌。
7. replay 是完整存档：`sentences.jsonl` + `meta.json`（冻结配置，续跑只读它）。
8. 指令词表（Directive.kind / host beat）归玩法包；brawl.stage 对 kind 不硬枚举
   （plan.yaml 按键查找，缺键 = 玩法包自洽 bug，PlanError）。
9. TTS 与演出同源：指令的 call_id（`{seq}.{speaker}`）即 manifest 键；audio_url 由壳的
   MediaResolver 解析填充，玩法不构造 URL。

## 跑起来

```bash
uv sync && (cd web && npm install)
uv run brawl avalon --player random --players 10 --percival --morgana --mordred --oberon --replay /tmp/avalon1
uv run brawl werewolf --player random --replay /tmp/ww1
uv run brawl console /tmp/avalon1
uv run brawl record /tmp/avalon1 --limit-seconds 30   # 出片段试看
uv run pytest -q
```

## 阅读顺序

1. `src/brawl/runtime/types.py` → `rules.py` → `loop.py` → `journal.py`：对局运行时边界。
2. `src/brawl/stage/stagecast.py` → `engine.py`：演出壳边界。
3. `src/brawl/games/avalon/rules_engine.py` + `stagecast.py`：一个完整玩法包长什么样（规则 + 演出）。
4. 玩法的 `tests/`：不变量如何被测试守住（投影测试 = random 整局真存档 + 自洽断言）。
5. One Brawl 定档（存档即游戏、stagecast 机制）与 Sentence Runtime 设计文档已删，见 git 历史。
