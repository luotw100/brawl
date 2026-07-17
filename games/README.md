# brawl-games

AI 直播对战的纯游戏逻辑层。`core/sr/`（Sentence Runtime）提供跨玩法运行时，`avalon/` 与 `werewolf/` 是两套自包含玩法。

语义全在玩法（rules 插件），管道全在 core：每个 AI 产出都是一条"句子"（Sentence），玩法规则负责解析、决定谁能听到、决定下一步该谁；core 只负责驱动循环、调用 OpenCode、按 channel 路由、落句子日志。**句子日志即 checkpoint**——一行一句 fsync，resume 就是重放 sentences，没有两路流对齐。

## 环境初始化

```bash
uv sync
```

TTS 推理不在本进程加载 Qwen3-TTS / PyTorch；需要自托管配音时，另起局域网 Qwen3-TTS API server，
再用 `--backend selfhosted --self-hosted-url http://<host>:<port>` 调用（音色读 meta 里的自托管 voice_id）。

## 跑一局

```bash
# 阿瓦隆（10 人全角色）
uv run brawl avalon --player random --players 10 --percival --morgana --mordred --oberon --replay /tmp/avalon1
# 狼人杀（12 人预女猎白）
uv run brawl werewolf --player random --replay /tmp/ww1

# 回放与配音（跨玩法，消费 replay/sentences.jsonl）
uv run brawl console /tmp/avalon1
uv run brawl tts /tmp/avalon1
# 或走自托管 Qwen3-TTS
uv run brawl tts /tmp/avalon1 --backend selfhosted --self-hosted-url http://192.168.232.31:7860
```

## 用 OpenCode 当玩家后端

Avalon / 狼人杀 CLI 自己托管一个 `opencode serve`：每座位一个 session，游戏引擎每回合把当前提问发过去，再按玩法约定解析成结构化决策。外部 server 连接模式已废弃。

```bash
# 先用固定支持版本登录一次；运行机还需安装 bubblewrap(bwrap)
opencode auth login

# CLI 启动、探活并回收本局专属 server；opencode 模式必须落 replay
uv run brawl avalon --player opencode --replay /tmp/avalon-opencode
uv run brawl werewolf --player opencode --replay /tmp/ww-opencode

# 可选：强制指定默认模型（providerID/modelID），空字符串则用 server 默认模型
uv run brawl avalon --player opencode --opencode-model kimi-for-coding/k2p7 --replay /tmp/avalon-opencode
```

当前固定支持 OpenCode `1.17.20`。CLI 用 `bwrap` 只把 `<replay>/_opencode` 映射给子进程，真实 replay 根目录不在其挂载空间；所有 agent 工具同时在 session 层禁用。每局生成的 workspace、OpenCode DB、XDG state/cache 和 server 日志都保留在 `_opencode/`，长期认证文件只读挂载，不复制进 replay。

模型选择优先级（单座位）：

1. `data/models.yaml` 里该座位的逻辑模型若配有 `opencode_model: providerID/modelID`，则按座位映射；
2. 否则退到 `--opencode-model`；
3. 都为空则让 OpenCode server 用其默认模型。

因此 `--player opencode --models oc-deepseek-v4-pro,oc-qwen3.7-plus,...` 可以给不同座位分配不同 OpenCode 模型；不配 `--models` 时随机摇到的 `oc-*` 模型也会自动映射到对应 OpenCode provider/model。

OpenCode 的可用模型可通过 `opencode models` 查询；`OpenCodeBackend` 会在每次 `POST /session/{id}/prompt_async` 时把该座位模型随 prompt 一起传给本局 server。

公开配置只保留 `opencode_model`；动态 URL 和随机密码属于进程内运行时数据，不接受外部注入。不需要 `.opencode.json`、自定义 agent 或 `games/scripts/`。

## 对局调试

- `brawl console <replay_dir>`：把 sentences.jsonl 渲染成终端面板（含"谁能听到"路由标签）。
- `brawl chat-view <llm_calls.jsonl> <行号>`：**旧格式 replay 专用**。新引擎不再本地落 llm_calls——每次模型调用的完整审计保留在 OpenCode 会话库里（`<replay>/_opencode/storage/opencode.db`）。

## 测试

```bash
uv run pytest -q   # 全套零 key、零网络（--player random / 纯函数）
```

## 配置优先级

直接构造 kwargs > CLI `--xxx` > 环境变量（Avalon 前缀 `BRAWL_AVALON_`、狼人杀前缀 `BRAWL_WEREWOLF_`）> `--config xxx.json` > 默认。字段见 `avalon/settings.py` / `werewolf/settings.py`（settings 即 CLI，flag 自动派生）。

## 框架分布

- `core/sr/types.py`：游戏盲类型——Delivery / Sentence / Expectation / ParseResult / HostEmit。
- `core/sr/rules.py`：玩法插件协议（Rules Protocol）——initial_state/expected/view/parse/apply/advance/pending/pack/unpack。
- `core/sr/loop.py`：驱动循环——speak+parse 同单元重试、按座位序确定性落日志、deliver 路由、resume 重放。
- `core/sr/journal.py`：replay 三件套——sentences.jsonl（句子日志）、meta.json、_snapshot.json（可选加速快照）+ avatars；不给 `--replay` 走 NullJournal。
- `core/sr/actor.py` / `opencode_backend.py`：OpenCodeActor（opencode session 适配）、RandomActor（零网络）、OpenCodeBackend（HTTP 协议）。
- `core/sr/console.py` / `tts.py`：跨玩法终端渲染与配音（消费 sentences.jsonl，对应 `brawl console` / `brawl tts`）。
- `core/sr/messages.py`：一次 agent 调用的消息类型（20 行，替代 langchain-core）。
- `core/registry.py` / `setup.py` / `threads.py` / `parsing.py` / `models.py` / `opencode_runtime.py`：注册表、边界解析（含人设唯一/可重复双模式）、席位线程、JSON 抽取、模型规格、opencode 托管生命周期。

玩法包（avalon/、werewolf/ 同构）：

- `rules_engine.py`：玩法全部语义——阶段机、计票/判负、信息隔离、prompt 构造、JSON 解析校验（Rules 实现）。
- `rules.py`：规则表（阵营/角色/板子/纯函数判负计数）。
- `state.py` / `view.py`：状态结构、结构化决策模型、喂给 actor 的视图对象。
- `prompts.py`：所有发给模型的文案（纯函数）。
- `validation.py` / `settings.py` / `registry.py`：配置边界（settings + registry + rng → GameSetup）、CLI 参数面、数据加载。
- `run.py`：入口装配（setup → actors → loop → journal）。
- `data/`：各自的 cast/hosts/models/strategies yaml，两玩法互不引用。

## 架构地图

见 `../.agents/skills/repository-overview/SKILL.md` 与 `../design/sentence-runtime.md`。

