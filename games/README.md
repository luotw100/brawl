# brawl-games

AI 直播对战的纯游戏逻辑层。`core/sr/`（Sentence Runtime）提供跨玩法运行时，`avalon/` 与 `werewolf/` 各自拥有规则、配置和内容数据。

玩法决定状态、信息隔离和输出格式；core 只负责调用 actor、路由 Delivery、按事务提交 Sentence。`sentences.jsonl` 是唯一 checkpoint，resume 会从头严格重放，不维护第二份快照。

## 安装

```bash
uv sync
```

## 新建对局

只有新建对局时可以指定玩家、角色、模型、人设、流派、随机种子、并发度和渲染开关等参数。

```bash
# 阿瓦隆：10 人全特殊角色
uv run brawl avalon \
  --player random --players 10 \
  --percival --morgana --mordred --oberon \
  --replay /tmp/avalon1

# 狼人杀：12 人预女猎白
uv run brawl werewolf --player random --replay /tmp/werewolf1
```

新 replay 目录必须不存在。创建后，`meta.json` 只允许写一次，其中冻结了续跑所需的玩法配置、逐座位 prompt 内容、模型引用和阿里云音色；后续修改玩法 YAML 不会改变这局。
同一 replay 同时只能有一个生成/续跑进程持有 journal 写权，第二个进程会明确失败，不会相互覆盖提交。

## 恢复对局

resume 只接受下面这一种命令形状：

```bash
uv run brawl avalon --resume --replay /tmp/avalon1
uv run brawl werewolf --resume --replay /tmp/werewolf1
```

不能附加 `--player`、`--models`、`--config`、`--no-render` 或任何其他参数。恢复路径只读取 replay，不读取当前 registry/YAML，也不会改写 `meta.json`。

## 回放与阿里云 TTS

```bash
uv run brawl console /tmp/avalon1
uv run brawl console /tmp/avalon1 --pace 0.01
uv run brawl tts /tmp/avalon1
```

TTS 保留可注入的 backend 协议供测试使用，但生产命令只使用阿里云 DashScope，音色只能来自 replay 中已冻结的 `setup.voices` / `setup.host_voice`。命令不提供 backend、voice 或 RPS 覆盖参数；增量结果写入 `tts/manifest.json`，并绑定具体模型 fingerprint、音色、文本与音频哈希，损坏或过期的单条缓存会自动补做。同一 replay 的并行 TTS 进程会直接拒绝。

凭证从环境变量或 `games/.env` 的 `DASHSCOPE_API_KEY` 读取。创建新音色也只走阿里云：

```bash
uv run brawl voice-forge '三十岁左右的清晰男声，语速沉稳' --name steady
```

## OpenCode 玩家

Avalon / 狼人杀 CLI 会为一局对局托管独立的 `opencode serve`。先安装当前项目支持的 OpenCode `1.17.20`、完成登录，并确保运行机有 `bwrap`：

```bash
opencode auth login
uv run brawl avalon --player opencode --replay /tmp/avalon-opencode
uv run brawl werewolf --player opencode --replay /tmp/werewolf-opencode
```

每座位的有效 `providerID/modelID` 在新局边界解析后写入 replay。优先使用该玩法 `data/models.yaml` 中逻辑模型的 `opencode_model`，否则使用新局的 `--opencode-model`；两者都为空才交给 OpenCode 默认模型。恢复时不能重新选模型。

本局的隔离 workspace、OpenCode DB、XDG state/cache 和 server 日志保存在 `<replay>/_opencode/`。完整模型调用记录以 OpenCode 会话库为准，games 不再额外生成旧式 `llm_calls.jsonl`。

## 配置与测试

新局配置优先级为：直接构造 kwargs > CLI > 环境变量 > `--config` JSON > 默认值。Avalon 环境变量前缀为 `BRAWL_AVALON_`，狼人杀为 `BRAWL_WEREWOLF_`。resume 不进入这套配置解析。

```bash
uv run pytest -q
uv run ruff check .
```

测试默认零 key、零网络。除了规则正向流程，还覆盖坏 replay、事务中断、冷恢复、信息隔离、严格输出格式、CLI 参数边界及 TTS 失败路径。

## 目录

- `core/sr/types.py`：Delivery / Sentence / Expectation / ParseResult / HostEmit。
- `core/sr/loop.py`：actor 调用、整轮事务、重试、路由和严格重放。
- `core/sr/journal.py`、`files.py`：create-once meta、原子 Sentence 日志和安全文件边界。
- `core/sr/actor.py`、`opencode_backend.py`：Random/OpenCode actor 适配和会话事务。
- `core/sr/console.py`、`tts.py`：只消费 replay 的终端回放与阿里云配音。
- `avalon/`、`werewolf/`：各玩法自己的规则机、prompt、状态、配置、测试和 `data/`；内容资产保持玩法自包含。
- `voicelab/`：阿里云音色铸造工具。
