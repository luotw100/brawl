# 设计文档：One Brawl —— 存档即游戏，演出即重放

> 合并 games/ 与 stage/ 为单一项目。replay 不再是两个系统间的接口契约，而是游戏存档；
> 演出（录屏）是把存档重放一遍并逐步投影成舞台指令。本文档是定档，替代
> `sentence-runtime.md` §8 的"阶段 6"与旧的"stage 消费 events.jsonl"契约。

## 0. 定档决策（本轮讨论拍板）

1. **单目录单项目**：不再有 games/ 与 stage/ 两个顶层系统、两个 uv 项目。一个 `brawl` 项目，一条 CLI。
2. **replay = 游戏存档**：`sentences.jsonl` + `meta.json`（+ tts/avatars 产物）是唯一真相。不引入 `stage.jsonl` 等派生文件。
3. **演出规则随玩法包**：`brawl record <replay>` 按 `meta.game` 加载对应玩法包的 stagecast（演出投影），纯函数、内存内完成，落盘为零。
4. **不兼容大改，零历史包袱**：events.jsonl 契约、旧 replay 兼容、sentences 桥、stage 侧玩法插件、7×24 频道栈——全部删除，不迁移。
5. **只做录屏**：直播/导播台/片库/队列/overlay 本期当不存在；record 与未来的直播是平级消费者，本期只交付 record。
6. **横屏 16:9 保持**；`brawl record` 一次一局；不做流式（不边生成边上屏），只消费完整存档。
7. **stagecast 是独立 Protocol**，不并入 Rules（loop 不感知舞台概念）。
8. `core/sr` 更名 `core/runtime`。
9. 前端本波不按玩法重组目录，只把 `protocol.ts` 按玩法拆文件、删除频道相关 UI。

## 1. 核心不变量（边界红线）

1. **"懂规则"只发生一次**：玩法包（`avalon/`、`werewolf/`）。聚合（票型/任务牌/狼刀结果）、配对（lady choose/announce）、台账（deaths/rejections/missions）只能出自 rules 的 state 或 action payload，任何壳代码不得重新计数、不得反解 `turn_id` 字符串格式。
2. **`core/` 不 import 任何玩法包**。玩法分发经 `core.stage.registry` 的注册表，装配发生在 CLI 入口层。
3. **stagecast 是纯投影**：`sentences + meta + media → [Directive]`。不碰节奏（plan/playplan 的事）、不碰 URL 构造（media resolver 的事）、不写盘。
4. **指令词表归玩法包**：`Directive.kind` 的取值集合由玩法包定义；`core/stage` 对 kind 不硬枚举（plan YAML 按键查找，缺键即玩法包自洽 bug，报 PlanError）。
5. **生成路径零改动**：`core/runtime`（loop/journal/actor/opencode/resume/tts/console）本次只搬家、不改语义。存档格式不变。
6. **录屏与（未来）直播同一份 tracks**：engine 产出确定性 StageBeat/AudioTrack，record 消费它拼音轨——这条旧优点保留。

## 2. 目录布局

```
brawl/                        # 仓库根 = 单一 uv 项目
├── pyproject.toml            # 依赖并集；[project.scripts] brawl = "brawl.cli:main"
├── core/
│   ├── runtime/              # 原 games/core/sr 整包 + games/core 平级模块
│   │   ├── types.py rules.py loop.py journal.py actor.py opencode_backend.py
│   │   ├── console.py tts.py files.py messages.py errors.py
│   │   ├── registry.py setup.py threads.py parsing.py models.py opencode_runtime.py
│   │   └── tests/
│   └── stage/                # 游戏盲演出壳
│       ├── protocol.py       # 信封基类：Directive/BaseScene/FeedItem/StateMsg/AudioTrack/
│       │                     #   NarrationCue/HostSpeech/GameResult（裁掉 Intermission/OverlayItem）
│       ├── stagecast.py      # Stagecast Protocol + StageGame bundle + registry
│       ├── media.py          # MediaResolver：call_id→音频 URL/路径/时长；seat→头像 URL
│       ├── engine.py         # 单局播放头（砍频道循环/队列/幕间/idle）
│       ├── playplan.py       # plan YAML → 确定性 beats/tracks（原样搬迁）
│       ├── server.py         # record 内部一次性服务：静态页 + /replay/* + /sfx/* + WS
│       ├── record.py record_settings.py
│       ├── cover.py cover_settings.py bgm.py sfx.py settings.py
│       └── tests/
├── avalon/                   # 玩法包 = 规则 + 演出
│   ├── rules_engine.py rules.py state.py view.py prompts.py
│   ├── validation.py settings.py registry.py run.py
│   ├── stagecast.py          # 【新】演出投影：存档 → 指令流
│   ├── protocol.py           # 指令/Scene 形状（原 stage/server/games/avalon/protocol.py）
│   ├── plan.yaml             # 节奏配方（原 stage/server/plans/avalon.yaml）
│   ├── data/ docs/ tests/
├── werewolf/                 # 同构
├── voicelab/                 # 音色铸造（原样搬迁）
├── brawl/
│   └── cli.py                # avalon|werewolf|console|tts|voice-forge|record|cover
├── web/                      # 前端（record 与未来的 stage 共用构建产物）
│   └── src/
│       ├── stage/            # Stage.tsx 等（裁掉 overlays/console）
│       ├── games/
│       │   ├── avalon.ts     # 玩法协议类型（protocol.ts 机械拆分）
│       │   └── werewolf.ts
│       └── useChannel.ts main.tsx
└── design/
```

## 3. 核心机制：stagecast = 存档重放 + 逐步投影

桥（stage/server/games/sentences.py）被迫重算票型、重数狼刀、正则反解 turn_id，
根因是它在存档**外面**够不着 state。同库之后，演出投影直接复用 resume 的重放路径：

```python
# avalon/stagecast.py（玩法私有；werewolf 同构）
def stagecast(sentences: list[Sentence], meta: dict, media: MediaResolver) -> list[Directive]:
    setup = load_setup(meta)                 # 与 resume 同源的 meta→setup
    rules = AvalonRules(setup)
    state = rules.initial_state(setup)
    out: list[Directive] = []
    for sent in sentences:
        post = rules.apply(state, sent.action)   # 与 loop/resume 完全同一条路
        out.extend(_project(sent, state, post))  # 纯函数：这句话 + 前后 state → 0..n 条指令
        state = post
    return out
```

由此，桥里每一处"第二遍懂规则"都有唯一归宿：

| 桥里的重算 | 新归宿 |
|---|---|
| `_AV_TURN`/`_WW_TURN` 正则反解 turn_id | 死。pre/post state 就是结构化进度 |
| pending_votes / pending_cards 攒组 | `state.votes` / `state.cards`（apply 已 fold），host payload 有 tally |
| 狼刀多数决重计票 | 死。死亡结果读 dawn/state；提议明细从 wolf_kill 句 action 取纯展示数据 |
| lady choose/announce 配对变量 | `state.lady_target` / `state.lady_seen` |
| succeeded/failed 手数计数器 | state 读 |
| deaths 台账 | state / host payload |
| `idiot_immune: pass`（迁就旧契约丢信息） | 玩法自行决定演不演 |
| `evil_chat` 未覆盖（桥直接 raise） | avalon/protocol.py 新增指令 kind，stagecast 必须覆盖 |

## 4. 协议

### 4.1 Stagecast（core/stage/stagecast.py）

```python
class MediaResolver(Protocol):
    """壳注入的媒体解析：语义键 → 传输事实。stagecast 只见此接口。"""
    def audio(self, call_id: str) -> tuple[str, float] | None: ...  # → (url, 秒)
    def avatar(self, seat: int) -> str | None: ...                  # → url（缺头像=None）

class Stagecast(Protocol):
    """一个玩法的演出投影能力。纯函数；不碰节奏、不碰 URL 构造、不写盘。"""
    def stagecast(self, sentences: list[Sentence], meta: dict, media: MediaResolver) -> list[Directive]: ...

@dataclass(frozen=True)
class StageGame:
    """玩法包向演出壳交付的全部能力（registry 按 meta.game 派发）。"""
    id: str
    stagecast: Stagecast
    new_scene: Callable[[], BaseScene]
    apply: Callable[[BaseScene, FeedItem], None]          # 展示态 reducer（原 plugin.apply）
    summary: Callable[[Directive, BaseScene], tuple[str, str, str]]  # 录制进度行
    is_opening_anchor: Callable[[Directive], bool]        # 录制首帧门
    plan_path: Path
```

- `call_id` 约定不变：`{seq}.{speaker}`，与 TTS manifest 键同源。Directive 基类带
  `call_id: str | None`；stagecast 经 `media.audio()` 填 `audio_url`，engine 汇总 durations 给 playplan。
- `GameResult`/终局判定：record 只认"stagecast 走完且末拍是玩法终局指令"。准入闸 =
  stagecast 纯函数走到底（半截局/坏局加载期即拒），替代旧 `is_broadcastable` 的 game_end 检查。
- registry：`core.stage.registry.register(StageGame)` / `game_for(game_id)`；
  `brawl/cli.py` 作为装配根 import 两个玩法包完成注册。

### 4.2 record 内部服务（core/stage/server.py）

一次性、127.0.0.1 随机口：静态 web 构建产物 + `GET /replay/*`（映射本局 replay 目录）
+ `GET /sfx/*` + `WS /ws`（推 StateMsg）。无 token、无导播台路由、无片库 API。
URL 是单向的：壳构造 URL 给浏览器；**音轨拼接不反解 URL**，record 从 resolver 直接拿
`replay_dir/tts/<file>` 绝对路径（旧 `audio_segments` 的 `/replays/` 前缀剥离随之死亡，
嵌套 replay id 的隐患同步消失）。

### 4.3 engine（单局化）

```python
async def play_replay(replay_dir, *, gate: PlaybackGate, publish, knobs) -> None:
    meta, sentences = load_save(replay_dir)          # meta.game 派发
    game = registry.game_for(meta["game"])
    media = MediaResolver(replay_dir, url_prefix="/replay")
    directives = game.stagecast(sentences, meta, media)   # 加载期失败 = 此局不可录
    beats = compile_stage_beats(directives, load_plan(game.plan_path), media.durations(), speed=knobs.speed)
    # 逐拍 emit（首帧门/hold_clock 语义不变）→ 终拍后返回；无幕间、无下一局
```

裁掉：queue / loop_pool / intermission / idle / library 扫描 / skip&step 之外的全部频道状态位。
保留：PlaybackGate（首帧门）、speed/feed_window/prefetch knobs、summary 进度行。

## 5. 死亡清单（不迁移，留 git 历史）

| 件 | 死因 |
|---|---|
| `stage/server/games/`（sentences.py 桥 + avalon/werewolf plugin + 217 行桥测试） | 知识重生在玩法包 stagecast/protocol/plan |
| events.jsonl 契约、`replay.load_events` 双格式、`VOICED` 表、旧 replay 兼容 | 契约消亡：演出直接投影存档 |
| `stage/server/replay.py` 大部（LibraryItem/library/tags/片库扫描） | 频道功能 |
| 频道栈：docker-compose.yaml、Dockerfile、edge/（caddy/frpc）、hub 导播台 UI（Console.tsx）、overlay.py、app.py 的 token/片库/队列路由、Intermission/OverlayItem、loop_pool/intermission knobs | 本期只做录屏；直播回归时在单局 engine 上重建调度层 |
| `record_settings` 的 events.jsonl 闸、`audio_segments` 的 URL 反解 | 跨系统残留，resolver 取代 |

## 6. 施工顺序（每步可跑可验）

**P0 — 定档（本文档）**。出口：评审通过，协议冻结。

**P1 — games 合并（纯机械，stage/ 不动）**
- 根 pyproject 落成；`games/core/sr` + 平级模块 → `core/runtime/`（`core.runtime.*` import 全量改写 `core.runtime.*`）；`games/{avalon,werewolf,voicelab}` → 根；`games/brawl/cli.py` → `brawl/cli.py`。
- stage/ 保持独立子项目原地可用（桥还活着，record 还能录旧路径）。
- 出口：`uv run pytest -q`（games 侧）全绿；`uv run brawl avalon --player random --replay /tmp/x` 生成正常；stage 测试原地全绿。

**P2 — stagecast（唯一新逻辑，stage/ 仍不动）**
- `avalon/stagecast.py` + `werewolf/stagecast.py`；`protocol.py`、`plan.yaml` 从 stage/server 复制改造进玩法包（旧件暂留，P3 删）。
- 投影测试：移植桥测试为投影测试；补 evil_chat / idiot_immune / lady 配对覆盖；自洽不变量——emit 的每个 kind 都被本包 plan.yaml 覆盖、spoken 句皆有 call_id、恰好一条终局指令收尾；三座真实崩局 replay 走到底。
- 出口：玩法包测试全绿（此时没有任何运行时消费 stagecast）。

**P3 — 壳迁移接线 + 删旧（一步完成切换）**
- `core/stage/` 落位：engine 单局化改造 + MediaResolver + server.py（app.py 极简版）+ record/cover/playplan/protocol 基类搬迁；record 闸改 stagecast dry-run、音轨拼接改 resolver 路径。
- engine 加载切到玩法包 stagecast；删 `stage/` 整个目录（桥/插件/频道栈随删）；web/ 平移到根并裁剪（console/overlay/intermission 删、protocol.ts 拆 `games/<game>.ts`）。
- `brawl record` / `brawl cover` 进 CLI；stage 旧测试改造随迁（test_avalon/test_werewolf 的 events fixtures 改为 stagecast 输出 fixtures；test_app 删除；test_engine/test_playplan/test_record/test_cover 改造保留）。
- 出口：单项目 `uv run pytest -q` 全绿；`brawl record <新 replay>` 端到端出片（人工验片）；`grep -r "events.jsonl" --include="*.py" .` 仅命中 design 历史文档。

**P4 — 文档定稿**
- README（根）、`repository-overview` skill、本目录设计文档索引更新；ruff 清零。
- 出口：新人按 README 一条命令出片。

## 7. 测试策略

1. **投影测试（玩法包，主力）**：fixtures 用真实 replay（既有三座崩局 + random 新局）。断言 kind 序列骨架、关键字段、§3 表格里的每条"不再重算"。
2. **自洽不变量（玩法包）**：stagecast 输出 × plan.yaml 双向覆盖；终局指令唯一且在末尾；audio 拍 call_id 可 join manifest（有 tts 时）。
3. **壳测试（core/stage）**：playplan 编译、engine emit/dwell/gate、record 的音轨分段与 mux 命令构造（沿用现有测试改造，directive 直接构造、不再过 events）。
4. **生成侧（core/runtime + 玩法 rules）**：现有测试原样随迁，语义零改动是 P1 的出口判据。

## 8. 不做的事（及回归条件）

- **直播/导播台/片库/overlay**：回归时 = 单局 engine 之上加调度层（取局/幕间/队列），stagecast/record 不动。
- **流式上屏（边生成边播）**：同库后技术上门已开（engine 订阅 loop committed 批），但按"只消费完整存档"纪律不做。
- **竖屏**：纯前端 layout 工程 + record 画布参数，不碰游戏/演出逻辑；需要时单独立项。
- **批量出片 / produce 串链**：先保持一次一局 + 四条命令，shell 自行串。
- **protocol.ts 从 pydantic 自动生成**：同库后可行的低悬果实，列为后续 tooling。
- **前端按玩法重组目录**：触发条件 = 第三玩法出现或某玩法要独立视觉体系。
