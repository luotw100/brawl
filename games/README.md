# brawl-games

AI 直播对战的纯游戏逻辑层。`core/` 提供跨玩法运行时，`avalon/` 保留阿瓦隆玩法规则、状态机和提示词。

## 环境初始化

```bash
uv sync
```

TTS 推理不在本进程加载 Qwen3-TTS / PyTorch；需要自托管配音时，另起局域网 Qwen3-TTS API server，
再用 `--backend qwen3-tts-self-hosted --self-hosted-url http://<host>:<port>` 调用。

## 跑一局

```bash
# 阿瓦隆：生成 replay 后，可继续在同一目录内生成发布素材
uv run brawl avalon --player random --players 10 --percival --morgana --mordred --oberon --lady --replay /tmp/avalon1
uv run brawl avalon-tts /tmp/avalon1
# 或走自托管 Qwen3-TTS
uv run brawl avalon-tts /tmp/avalon1 --backend qwen3-tts-self-hosted --self-hosted-url http://192.168.232.31:7860
```

## 用 OpenCode 当玩家后端

Avalon / 狼人杀 CLI 自己托管一个 `opencode serve`：每座位一个 session，游戏引擎每回合把私人历史和当前提问发过去，再按玩法约定解析成结构化决策。外部 server 连接模式已废弃。

```bash
# 先用固定支持版本登录一次；运行机还需安装 bubblewrap(bwrap)
opencode auth login

# CLI 启动、探活并回收本局专属 server；opencode 模式必须落 replay
uv run brawl avalon --player opencode --replay /tmp/avalon-opencode
uv run brawl werewolf --player opencode --replay /tmp/ww-opencode

# 可选：强制指定默认模型（providerID/modelID），空字符串则用 server 默认模型
uv run brawl avalon --player opencode --opencode-model opencode-go/deepseek-v4-pro --replay /tmp/avalon-opencode
```

当前固定支持 OpenCode `1.17.18`。CLI 用 `bwrap` 只把 `<replay>/_opencode` 映射给子进程，真实 replay 根目录不在其挂载空间；所有 agent 工具同时在 session 层禁用。每局生成的 workspace、OpenCode DB、XDG state/cache 和 server 日志都保留在 `_opencode/`，长期认证文件只读挂载，不复制进 replay。

模型选择优先级（单座位）：

1. `data/models.yaml` 里该座位的逻辑模型若配有 `opencode_model: providerID/modelID`，则按座位映射；
2. 否则退到 `--opencode-model`；
3. 都为空则让 OpenCode server 用其默认模型。

因此 `--player opencode --models oc-deepseek-v4-pro,oc-qwen3.7-plus,...` 可以给不同座位分配不同 OpenCode 模型；不配 `--models` 时随机摇到的 `oc-*` 模型也会自动映射到对应 OpenCode provider/model。

OpenCode 的可用模型可通过 `opencode models` 查询；`OpenCodeBackend` 会在每次 `POST /session/{id}/prompt_async` 时把该座位模型随 prompt 一起传给本局 server。

公开配置只保留 `opencode_model`；动态 URL 和随机密码属于进程内运行时数据，不接受外部注入。不需要 `.opencode.json`、自定义 agent 或 `games/scripts/`。

## 测试

```bash
uv run pytest -q   # 全套零 key、零网络（--player random / 纯函数）
```

## 配置优先级

直接构造 kwargs > CLI `--xxx` > 环境变量（Avalon 前缀 `BRAWL_AVALON_`、狼人杀前缀 `BRAWL_WEREWOLF_`）> `--config xxx.json` > 默认。字段见 `avalon/settings.py` / `werewolf/settings.py`（settings 即 CLI，flag 自动派生）。

## 框架分布

- `core/actor.py`：通用 agent 后端（OpenCode / random）抽象、调用、重试、`llm_calls` 落盘。
- `core/registry.py` / `setup.py`：模型/人设/流派/主持人注册表，以及通用边界解析。
- `core/threads.py` / `host.py`：每席位私有线程、公开广播、主持人口播事件。
- `core/recorder.py`：replay 目录写入与空对象 recorder。
- `core/resume.py`：checkpoint 数据库路径、sidecar 水位和续跑截断点。
- `core/runner.py`：LangGraph 事件流驱动与 checkpoint 水位对齐。
- `core/parsing.py` / `events.py` / `models.py`：JSON 抽取、事件 ID/emit、模型规格。

## 架构地图

见 `../.agents/skills/repository-overview/SKILL.md`。
