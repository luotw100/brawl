# 设计文档：句子运行时（Sentence Runtime）

> 丢弃 LangGraph，把"玩法规则"与"运行基建"彻底分层。玩法只懂规则，基建只懂管道。

## 1. 目标

- **去掉 LangGraph**。它的价值（节点编排 + checkpointer）在本项目里没有不可替代性，反而带来了两路流对齐（sidecar / watermark / truncate）的脆弱性。
- **建立一条贯通两个玩法的抽象**：所有 AI 产出的东西都是一个"句子"。玩法规则负责解析句子、决定谁能听到、决定下一步该谁、决定给某席位注入什么 prompt。core 只负责驱动循环、调用 opencode、记录与路由句子、resume / replay。
- **消灭评审里三条债务**：`quest`/`attempt` 词汇泄漏、`build_player`/`run.py` 逐字复制、resume 的 sidecar-watermark 脆弱性。

## 2. 核心不变量（边界红线）

1. **core 搜不到任何玩法名词**。不能出现 `quest` / `attempt` / `kill` / `vote` / `seer` 等。core 不解析句子内容，不知道 channel 之外"这句话什么意思"。
2. **路由（谁听得到）由 rules 用代码声明**，绝不由 host LLM 或任何模型推理决定。信息隔离压在纯代码拓扑上，不压在模型行为上。
3. **prompt 文本由 rules 生成**（`rules.view`）。core 只把 prompt 原样发给 opencode，不套通用模板——模板里写"你是 3 号狼人……"就是泄漏。
4. **句子日志 = checkpoint**。一行一句，写盘即推进。resume 不依赖两路流的实测顺序。

## 3. 分层与职责

```
┌──────────────────────────────────────────────────────────┐
│ rules (玩法插件，纯 Python)                                │
│   expected / view / parse / topology / apply / advance    │
│   is_terminal / turn_id / initial_state / pack / unpack   │
│   —— 唯一懂语义的地方                                     │
├──────────────────────────────────────────────────────────┤
│ core (基建，游戏盲)                                        │
│   驱动循环 / opencode 调用 / recorder / looper-resume     │
│   Delivery 路由 / Sentence 日志 / state 快照              │
│   —— 只搬字节，按 channel 写 inbox                        │
├────────────────────────────────────────ological──────────┤
│ stage (直播回放，消费快照)                                │
│   读 Sentence 日志或经一层 event renderer → 主持人/画面    │
└──────────────────────────────────────────────────────────┘
```

一句话：**语义全在 rules，管道全在 core**。

## 4. 类型与协议

### 4.1 core 侧（游戏盲）

```python
# core/types.py
Channel = int | Literal["broadcast", "audience"]
#   int      -> 写入该 seat 的 inbox
#   broadcast-> 写入所有 seat 的 inbox
#   audience -> 写入观众上帝视角流（不进任何 seat）

class Delivery(BaseModel):
    text: str                 # 别人实际听到的自然语言。可空（沉默动作）
    channels: list[Channel]   # 排序去重后 deterministic

class Sentence(BaseModel):
    turn_id: str              # rules.turn_id(state) 给；core 当字符串用
    speaker: int | Literal["host"]   # 席位号，或主持人
    raw: str                  # opencode 原始响应（给 replay / llm_calls join 存原文）
    deliveries: list[Delivery]
    action: str               # opaque：rules.parse 的结构化结果，core 当 bytes 存
                              # 序列化由 rules 负责；core 只 append + apply
    seq: int                  # 全局自增序号（core 赋值，日志用）
```

`action` 是 **opaque token**：序列化成什么由 rules 决定（JSON 也好 dataclass 也好），core 只 `state = rules.apply(state, sent.action)`，从不读它。

### 4.2 rules 侧（玩法插件协议）

```python
# core/rules.py —— Protocol，玩法实现
class Rules(Protocol):
    # state 完全 opaque：core 不读不写，由 rules 管理
    State = TypeVar("State")

    def initial_state(self, setup: GameSetup) -> State: ...
    def is_terminal(self, state: State) -> bool: ...

    # 该谁、要什么 —— 一次"期望"
    def expected(self, state: State) -> Expectation: ...
        # Expectation = { seat, prompt, schema(给玩家的JSON约束), turn_id }

    # 给某席位注入 prompt 文本（含私密信息隔离）
    def view(self, seat: int, state: State) -> str: ...
        # 返回最终 prompt 文本。core 原样发给该 seat 的 opencode

    # 解析 + 校验玩家产出 —— 唯一懂语义的地方
    def parse(self, raw: str, state: State, exp: Expectation) -> ParseResult: ...
        # ParseResult = { deliveries, action }
        # 校验失败 -> rules 抛 ValueError，core 重试同一 expectation

    # 拓扑：这句话让谁听到（已可由 parse 结果给出，也可由 rules 在 parse 时算）
    # 约定：channels 直接写进 ParseResult.deliveries，不再单列 topology()

    # 纯 fold：吃一句话，推进状态。tt诉说/计票/胜负都在这里
    def apply(self, state: State, action: str) -> State: ...

    # 下一阶段或终局推进（若不内嵌在 apply 里）
    def advance(self, state: State) -> State: ...

    # 回合定位 —— 替代旧 decision_id 的 quest/attempt
    def turn_id(self, state: State) -> str: ...   # "quest3.attempt2.vote" / "day2.wolf_kill"

    # state 持久化（resume 用）—— 亦是 opaque
    def pack(self, state: State) -> bytes: ...
    def unpack(self, raw: bytes) -> State: ...
```

`Expectation` 与 `ParseResult` 是 core↔rules 的固定结构；除此之外 core 不触碰 state/action 的任何字段。

## 5. 驱动循环（整个引擎）

```python
# core/loop.py
async def run(rules: Rules, setup: GameSetup, actors, log, *, recorder):
    state = rules.initial_state(setup)
    while not rules.is_terminal(state):
        exp   = rules.expected(state)
        prompt= rules.view(exp.seat, state)
        raw   = await actors[exp.seat].speak(prompt, exp.schema)   # opencode 调用
        pr    = rules.parse(raw, state, exp)                      # 失败 -> 重试同一 exp
        seq   = recorder.next_seq()
        sent  = Sentence(turn_id=exp.turn_id, speaker=exp.seat, raw=raw,
                        deliveries=pr.deliveries, action=pr.action, seq=seq)
        recorder.append(sent)            # fsync 一行 = 一次原子 checkpoint
        deliver(pr.deliveries, threads)  # 按 channel 写 inbox，core 唯一懂路由的活
        # 可选：每 N 句压一份 state 快照加速 resume
        state = rules.advance(rules.apply(state, pr.action))
    recorder.finalize(rules.pack(state))
```

旧 LangGraph 的对等物：`drive_graph` + `astream(custom/values/checkpoints)` + `_route_after_*` 全部塌缩成上面这个 while。`route_after_*` 那些纯函数直接搬进 `rules.advance`。

## 6. 信息隔离如何被本模型保证

- 私密信息的流向**在 `rules.parse` 里**就被钉死成 `Delivery.channels`：例如狼人夜晚刀人 → `channels = [其他狼的 seat, audience]`，好人席位的号永远不进 channels。
- core 的 `deliver` 是**只按 channels 写**的机械函数，无任何裁量权；物理上无法把私密话写进非授权 inbox。
- 内心独白（audience-only）→ `channels = ["audience"]`，写进观众流，不进任何 seat。
- 沉默动作（女巫救人）→ `channels = [self, "audience"]`，自己听见自己 + 观众上帝视角。
- `rules.view` 是另一个闸口：构造某席位 prompt 时，只能从该席位"有权看到"的状态子集里取。这是"看得到什么"的隔离；`channels` 是"说出去给谁"的隔离。两者合起来覆盖完整边界。

设计完成性测试（施工时写一条 invariant 断言）：**对任意 Sentence，对任意非授权 seat s，s 的 inbox 文本与该 Sentence.deliveries 互不相干**。这条不变量今天由 `SeatThread` 结构保证，明天由 `channels` 保证——形变不变质。

## 7. resume / replay：句子日志即真相

```python
# resume：启动时
sentences = recorder.load_sentences()
if checkpoint := recorder.latest_checkpoint():        # 可选加速
    state = rules.unpack(checkpoint.bytes)
    replay = sentences.after(checkpoint.seq)
else:
    state = rules.initial_state(setup); replay = sentences
for sent in replay:
    state = rules.apply(state, sent.action)          # 重放
# 继续主循环
```

- **没有两路流对齐**：只有一路 sentences，无 sidecar / watermark / "custom 先于 checkpoint" 的实测顺序依赖。
- **torn 超步不存在**：句子是原子单位，写半行不会发生（fsync 后才递进 seq）。
- **replay = 从头重放 sentences**：无需 sqlite saver，无需 LangGraph 的 checkpointer ABI。`state.sqlite` 退化为"可选的加速快照表"，不是正确性的承重墙。
- event↔llm_call join：`Sentence.turn_id + seq` 当 join key，替代旧 `decision_id(quest, attempt, phase, seat)`。`quest`/`attempt` 从 core 彻底退场。

## 8. stage 衔接（已定：B —— 观众事件即同一流句子）

**拍板**：rules 在关键节点主动 emit 观众事件，事件本身是带 `channels=["audience"]` 的 Sentence，与发言、TTS 同走一条日志总线。

含义：

- **一路总线**。道白、夜晚揭示、计票、胜负、主持人 toast 全是 Sentence，`speaker` 可能是 seat 也可能是 `"host"`，区别只在 `channels` 与 `action` 的 payload shape。
- **TTS 与事件同源**。TTS 消费同一 audience 流，不再维护两份"哪些事件要配音"名单（杀掉评审里那条 `VOICED` 表与 `games/avalon/tts.py` 的重复契约 drift 隐患）。
- **stage 只消费 audience channel**。stage 不需要独立的"拖车事件源"ADAPTER；它读 audience 流，该渲染什么由 Sentence 的结构化 payload 决定。
- **肃清边界**（施工注意）："结构化事件 payload" 与 "自然语言话" 挤在同一条日志里，要给 Sentence 一个明确判别字段，避免观众流被当成道白回喂进某席位 prompt。建议：`action` 解析时若 payload 是"事件型"（reveal / tally / win），其 `deliveries.text` 就是面向观众播报的自然语言（如主持人念出"3 号玩家是莫德雷德"），而 `action` 里存结构化部分（`{"reveal": {"seat":3,"role":"mordred"}}`）。席位是 LLM，它听到的是 `deliveries.text`；观众/stage 是结构化消费者，它读 `action`。两条读者各取所需，同一条 Sentence 服务两端。
- **加第三玩法**：观众事件 schema 扩到新玩法时，只是新 rules 往同一条流里 emit 新的 audience Sentence，不碰 stage 的游戏无关层（前提是 stage 对承载 payload 的字段是开放/通配的，不在 stage 侧硬枚举事件类型——这条补进 stage 阶段 6 的约束）。

## 9. 施工顺序（strangler，每步可跑可验）

> 原则：新引擎与旧 LangGraph core 并存到阶段 5 才删除旧码。全程保留"旧玩法能跑"作为对照。

**阶段 0 — 设计冻结（本文档）**
- 评审/补本协议签名（Expectation / ParseResult / Sentence / Rules）。
- stage 衔接已定（§8 B 方案：观众事件即带 `channels=["audience"]` 的同一流 Sentence）；host 已定（纯函数模板，见阶段 3）。
- 出口：文档评审通过，协议冻结。

**阶段 1 — core 骨架（新增，不碰旧）**
- 新增 `core/types.py`（Delivery / Sentence / Channel）、`core/rules.py`（Rules Protocol）、`core/loop.py`（驱动循环 stub）、`core/looper_recorder.py`（Sentence 日志 + 可选快照）。
- 复用现成：`core/opencode_runtime.py`、`core/actor.py` 的 opencode 调用部分抽出来给 loop 用；`core/threads.py` 的 `SeatThread` 留用（其 broadcast/commit 语义和 channels 完全合拍）。
- 出口：一个 `DummyRules`（三步结束）跑通 loop，能写 Sentence 日志、能 resume。

**阶段 2 — Avalon rules 翻译（最大块）**
- 现状规模：`avalon/nodes.py` 467 + `graph.py` 48 + `state.py` 161 + `run.py` 186 + `player.py` 223 + `prompts.py` 600 + `validation.py` 143。
- 把每一类 LangGraph 节点落进 rules 方法：
  - 发言/投票/组队/刺杀节点 → `expected` + `view` + `parse`（含结构校验，沿用现有 ValueError 硬校验）。
  - `route_after_*` / `check_node` / 计票 / 胜负 → `apply` / `advance` / `is_terminal`。
  - `threads` 的广播/`host.announce` → 在 parse 里产出 Delivery（host 串场词作为 `speaker="host"` 的 Sentence）。
  - `prompts.py` 的 host/user 模板 → 搬进 `rules.view`，按席位拼最终 prompt（基本是搬运。host 串场仍可走模板，见下）。
- 不重写引擎，逐节点翻译；保留旧 `nodes.py`/`graph.py` 作回退。
- 出口：Avalon 在新引擎下完整跑通一轮所有阶段；smoke 跑通。可对照旧引擎同种子产出做一致性抽查。

**阶段 3 — host 角色定位（已定：rules 纯函数模板）**
- **拍板**：host 是 `rules` 里的纯函数，串场词由模板生成，作为 `speaker="host"` 的 Sentence，`channels` 由给定拓扑决定（reveal → broadcast，夜晚结果 → private seer）。**不**在循环里引入第二个 LLM actor。
- 现 `games/avalon/prompts.py` 的 host 模板直接搬进 rules 的串场词构造点（归 `rules` 的等价方法，可能并入 parse/advance 里产出 host Sentence）。
- 预留升级口子：若日后直播感不足要 LLM 润色，再引入"非玩家 actors"循环位置，不动 rules 的拓扑与 parse 契约。
- 出口：阶段 2 的 host 串场词来源固定为 rules 纯函数，contracts 定稿。

**阶段 4 — 旧 LangGraph core 删除**
- 删 `core/runner.py`（drive_graph/graph_config）、`core/resume.py`（sidecar/watermark）、`core/events.py`（decision_id/emit 的旧形）、`core/actor.py` 里 LangGraph 耦合的 AgentActor 部分（保留 opencode 调用核心）。
- 删两个玩法的 `graph.py`、`nodes.py`、旧 `run.py` 的 `_drive_*` / `build_graph` 调用。
- 出口：grep 不到 `langgraph`、`quest`、`attempt` 在 core/。两玩法仅靠新 loop 跑。

**阶段 5 — Werewolf rules 移植**
- 现 `werewolf/nodes.py` 458 + `graph.py` 65 + `state.py` 159 + `run.py` 161 + `player.py` 332 + `prompts.py` 302 + `validation.py` 116。
- 复用阶段 2 沉淀的拓扑四件套（broadcast/group/private/audience-only/solo）。夜晚角色能力（预言家/女巫/猎人）的私密信息走 `channels` + `rules.view` 双闸口。
- 顺带：补 werewolf 的 replay/tts 子命令（评审里缺的）。
- 出口：Werewolf 在新引擎跑通；replay/tts CLI 对齐 Avalon。

**阶段 6 — stage 衔接（B 方案落定）**
- 按 §8：stage 消费 audience channel 的 Sentence 流。结构化事件走 `action` payload，道白走 `deliveries.text`，同一条 Sentence 服务"席位 LLM 听 text"与"观众/stage 读 payload"两端。
- stage 游戏无关层不动，只把消费源从旧 `events.jsonl` 切到新 audience 流；约束 stage 侧对 audience payload 不硬枚举事件类型。
- 出口：一个已录制 replay 能在新日志格式下被 stage 回放。

**阶段 7 — 清扫与文档**
- 删 `core/setup.py` 里 `resolve_unique_personas` 的硬约束，改成"唯一 / 允许重复"双模式（werewolf 不再 fork）。
- `build_player` 收进 core 的 helper，消除两玩法 150 行复制。
- 更新 `repository-overview` skill 与 CLAUDE。

## 10. 预计改动范围

| 文件 | 行数 | 动作 | 备注 |
|---|---|---|---|
| `core/types.py` | — | 新增 ~60 | Delivery/Sentence/Channel/Expectation/ParseResult |
| `core/rules.py` | — | 新增 ~40 | Rules Protocol + GameSetup |
| `core/loop.py` | — | 新增 ~80 | 驱动循环 / resume |
| `core/looper_recorder.py` | — | 新增 ~100 | Sentence 日志 + 快照，替代 sidecar |
| `core/actor.py` | 440 | 重构 | 剥离 LangGraph 耦合，opencode 调用核心留用；`_record` 的 quest/attempt 删除 |
| `core/runner.py` | 79 | **删** | drive_graph/graph_config 退役 |
| `core/resume.py` | 85 | **删** | sidecar/watermark 退役（逻辑移入 loop/looper_recorder） |
| `core/events.py` | 28 | **删/改** | decision_id 退役，turn_id 由 rules 提供 |
| `core/threads.py` | 44 | 原地小改 | 保留 SeatThread；`commit` 可能并入 deliver |
| `core/opencode_runtime.py` | 364 | 原地不动 | sandbox 生命周期与抽象无关 |
| `core/host.py` | 19 | **删或并入 rules** | host 是 rules 内部，不再独立 core 抽象 |
| `core/registry.py` / `setup.py` | 78 / 79 | 小改 | persona 双模式；build_player helper 下沉 |
| `avalon/rules*.py` | — | 新增 ~700 | rules 实现（从 nodes/graph/prompts 翻译） |
| `avalon/nodes.py` | 467 | **删** | 落入 rules |
| `avalon/graph.py` | 48 | **删** | 落入 rules.advance |
| `avalon/run.py` | 186 | 大幅瘦身 ~150 | 剩 settings/CLI 入口，跑 loop |
| `avalon/player.py` | 223 | 瘦身 | build_player 主体抽进 core |
| `avalon/prompts.py` | 600 | 重整 | host/user 模板搬进 rules.view；体量不变但归属变 |
| `avalon/state.py` | 161 | 小改 | 纳入 rules state，其私有字段不变 |
| `avalon/replay.py` `tts.py` | 227 / 508 | 小改 | 改读 Sentence 日志 |
| `werewolf/*` | 同构 | 同上 | 阶段 5，规模略小于 Avalon |
| `stage/server/replay.py` | 166 | 改 | 改读 audience channel Sentence 流 |

**代码量级感**：净增约 +1000（新 core ~280 + Avalon rules ~700），净删约 -1100（nodes/graph/runner/resume/events/run 脚手架 + 复制体），**净减少**。复杂度收益主要在"两玩法共享一套 ~280 行基建"而非今天的两套 ~150 行脚手架。

## 11. 风险与待决

1. **prompt 模板体积**：`prompts.py` 600 行（host + user）。搬进 `rules.view` 是搬运不是重写，但要重新组织成"按席位 + 按阶段"的纯函数，别在搬运中引入 bug。建议阶段 2 先搬运、后优化。
2. **retry 语义**：旧引擎靠 LangGraph 超步重试。新 loop 的重试 = 重新对同一 `expected` 调 opencode。需明确重试上限、失败轮的消息清理（呼应最近 `fix(opencode): retry 前清理失败轮消息` 这条提交）。
3. **opencode session 与"该谁说话"的耦合**：旧 `OpenCodeBackend._session_locks` 按 seat 维护会话。新 loop 每个 seat 一个 actor，天然一一对应，竞态（评审 minor #6）顺带消失。
4. **SQL/日志过渡**：阶段 2 搬运时先按旧路径保留 `events.jsonl` 写法（可由 `action=payload` 的 audience Sentence 透出），让 stage 不必等到阶段 6 才能跑——避免中间阶段 stage 完全失联。
5. **state 序列化**：`pack/unpack` 要求 rules state 可序列化。Avalon 现有用 pydantic，可行；但 `SeatThread` 含消息历史，体积会随对局长，快照节流策略要在阶段 1 定。
6. **回放确定性**：重放 `rules.apply` 必须与实时跑结果一致。任何随机必须前置到 `initial_state`；旧引擎这条做对了，迁过去不能破。

## 12. 不做的事

- 不引入新的编排框架替换 LangGraph（alan/Temporal 之类）。就是 `async while` + 一张句子表。
- 不把 rules 写成 DSL/YAML。rules 是普通 Python，用 import 结构组织，便于调试与断点。
- 不为"支持非卡牌游戏"过度泛化。抽象卡到"句子 + 期望 + 拓扑"是为这两个玩法服务；若以后来个实时微操游戏，再开第三次抽象。