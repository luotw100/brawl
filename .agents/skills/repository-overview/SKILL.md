---
name: repository-overview
description: brawl 仓库架构全景。AI 直播对战平台（让人设 AI 互相玩谁是卧底/狼人杀/辩论）的项目背景、技术架构、目录指路、核心不变量、阅读顺序。
---

# brawl 仓库架构

这是一张**指向代码的地图**，不是代码的镜像。背景、思路、不变量写在这里；具体字段/参数以代码为准，按指针去读源文件。**只描述当前状态**，不写迁移史。改文档前先比对代码。

## 项目背景

`brawl` 是一个 **AI 直播对战平台**：让若干常驻人设 AI 互相玩社交推理类游戏（谁是卧底、狼人杀、AI 辩论……），目标是 7×24 无人值守直播。观众全程**上帝视角**（看得到所有人的词和身份），靠押注（押平民/卧底赢、押每轮谁出局）和赛后积分榜参与互动。

**当前进度**：已有两个玩法的纯游戏逻辑层，均能跑通、有测试、llm/random 双后端——**谁是卧底**、**阿瓦隆**（全角色：梅林/派西维尔/忠臣/刺客/莫甘娜/莫德雷德/奥伯伦/爪牙 + 湖中仙女流程模块）。**演出层 `stage/` 已实现**（阿瓦隆 replay → 直播画面 + 导播台，见 `stage/README.md`）；直播平台对接（抖音弹幕/礼物/押注）与 TTS 生成**尚未开始**。

**核心定位：`games` 是"对局快照生成器"，不是直播实时引擎。** 跑一遍 CLI 把一局**离线生成、冻成 replay**（落盘的事件时间线），直播时**回放**这份 replay 而非实时跑模型——实时跑 LLM 延迟/失败/成本/不可重来都不可控。因此生成器里**没有任何 wall-clock**（不 sleep、不计时）：尽快跑完一局即可；"每阶段停留多久、真人弹幕押注"等**节奏与真人互动属于将来的回放/直播层**，不在生成器。

## 一句话总览

顶层是**按关注点切分的组件 monorepo**，组件之间靠"结构化事件流"解耦：`games`（游戏逻辑，已实现）把一局产出为**事件流快照（replay）**，未来的 `stage`（画面）和 `live`（直播/抖音互动）回放/订阅同一条流。`games` 下**一个文件夹一个玩法**，彼此自包含、互不 import。

## 仓库布局

```
brawl/
├── games/                       # 【已实现】纯游戏逻辑，一个独立 uv 项目
│   ├── pyproject.toml           # 依赖 + 阿里源镜像 + console script `brawl`
│   ├── .env(.example)           # API key（不提交）；settings.py 启动时 load_dotenv 加载
│   ├── cli.py                   # 纯子命令路由：认出玩法，把剩余 argv 转给它，不认识任何 flag
│   ├── undercover/              # 谁是卧底（自包含）
│   │   ├── settings.py          # UndercoverSettings（配置唯一真源，本身即 CLI：pydantic-settings 自动派生 flag）
│   │   ├── registry.py          # 加载 data/：ModelSpec / Persona / Strategy / 词库
│   │   ├── state.py             # GameState / Seat / Role / Utterance / Describe / Vote
│   │   ├── deps.py              # Deps：注入图的"外部世界"(player/registry/rng)
│   │   ├── player.py            # Player 抽象 + LLMPlayer / RandomPlayer + PlayerView
│   │   ├── prompts.py           # 描述/投票提示词（绝不告诉 AI 自己身份）
│   │   ├── validation.py        # 边界：settings+registry+rng → 校验过且已摇定的 GameSetup
│   │   ├── nodes.py             # 裁判节点（纯代码）：开局公布/轮转/计票/判胜负（无兜底，出错即抛）
│   │   ├── graph.py             # LangGraph 装配
│   │   ├── events.py            # emit()：经 LangGraph 自定义流往外推 god-view 事件
│   │   ├── console.py           # rich 渲染器（订阅事件流；stage 的终端版雏形）
│   │   ├── parsing.py           # 从模型自由文本里稳健抽 JSON + pydantic 校验
│   │   ├── recorder.py          # Recorder：--replay <dir> 落盘一局产物（events/llm_calls/meta）
│   │   ├── run.py               # 一局编排：座位映射→跑图→事件扇出给渲染器 + 录制器
│   │   ├── data/                # models.yaml / cast.yaml / strategies.yaml / words.yaml（可调内容）
│   │   └── tests/               # 该玩法的 pytest 套件（随玩法自包含；见下「测试」）
│   ├── avalon/                  # 阿瓦隆（自包含，逐文件与 undercover 同构）
│   │   ├── rules.py             # 标准规则常数+纯规则函数：队伍人数/红方数/破坏票门槛/夜晚可见性矩阵 sees·night_intel
│   │   ├── player.py            # 仅一个 LLMPlayer + 注入式 ModelBackend（LiveBackend 真模型 / FakeBackend 离线假后端）；不再有独立 RandomPlayer
│   │   ├── host.py              # 主持人：非 Player（无视角/不决策/零 LLM），announce 一次完成"广播进全员线程 + emit host 事件"
│   │   ├── …                    # settings/registry/state/deps/prompts/parsing/validation/nodes/graph/events/console/recorder/run 同名对应
│   │   ├── data/                # models.yaml / cast.yaml / strategies.yaml（无词库）
│   │   └── tests/
│   └── …                        # 加玩法=并列一个自包含文件夹（含自己的 data/ 与 tests/）
├── stage/                       # 【已实现】演出层：replay → 直播画面（独立 uv 项目，详见 stage/README.md）
│   ├── server/                  # FastAPI：engine(播放头/队列/循环池/幕间/暂停跳局)、scene(阿瓦隆 adapter：
│   │                            #   adapt 事件→指令 + apply 折快照，不变量"快照≡增量回放")、overlay 画布、
│   │                            #   hub(WS 广播)、app(控制 API，Bearer token)、settings(BRAWL_STAGE_*)、tests/
│   ├── web/                     # Vite+React+TS：/#/stage 观众页(1080×1920 竖屏等比缩放，OBS 捕获它)
│   │                            #   + /#/console 导播台（排片/节奏/overlay 挂撤/预览）
│   ├── docker-compose.yaml      # 一个栈=一个频道（状态全在服务端，人人同画面）；多栈并行靠 env 文件
│   └── edge/                    # 公网 https profile：Caddy(DNS-01) + frpc 外连中继（与 wangzonghao 共用 frps）
└── live/                        # 【未做】直播平台接入：抖音 API/弹幕/观众押注
```

## 技术架构

- **Python 3.12 + uv**；依赖装包走**阿里云 PyPI 镜像**（`games/pyproject.toml` 的 `[[tool.uv.index]]`）。
- **编排用 LangGraph**：一局 = 一个状态机。**裁判节点是纯代码**，AI 只决定"说什么"，绝不决定"何时说"。
- **LLM 走 langchain-openai**（`ChatOpenAI` 普通补全），不手搓 HTTP。**结构化输出靠 prompt 约束 JSON + 自己解析校验**（`undercover/parsing.py`），**刻意不用 `with_structured_output`**——各厂商对 `response_format`/`tool_choice` 支持不一，自解析最通用。所有厂商都是 OpenAI 兼容端点，换厂商只改 `data/models.yaml`：每模型可配 `base_url`/`timeout`/`extra_body`。我们这种简单任务**关掉模型思考**（`extra_body: {thinking: {type: disabled}}`，DeepSeek 实测 reasoning→0、又快又省）；prompt 里强调简短输出。当前模型池只放有 key 的 `deepseek-v4-flash`/`deepseek-v4-pro`。
- **不做画面/直播**：`games` 只吐结构化事件流；`stage`/`live` 是将来订阅方。

## 核心不变量

看单文件推不出来、动手前必须对照的架构决策：

1. **私密信息通道绝对干净：每座位的私有信息只进该座位自己的 prompt 和观众事件流，绝不进 AI 之间可见的公开记录。** 卧底的形态：**没有人被告知自己的身份**——prompt 只给一个词，让 AI 从别人描述里自己推断是不是异类（`undercover/prompts.py` 绝不泄露"你是多数派/少数派"），这是全部戏剧张力的来源。阿瓦隆的形态：自己的角色与夜晚情报**自己知道**（这是规则），但别人的角色、谁出了破坏票永远保密——公开账本 `Utterance`/`ProposalRecord` 的 schema 上就没有这些字段，任务只公开破坏票**张数**。
2. **内心独白只给观众，绝不进别人上下文。** 数据上靠两条独立通道保证：`state.Utterance`（AI 之间可见的公开记录）**不含** `monologue/role/word`；独白只走 `events.emit` 出向流给渲染层。测试 `test_transcript_never_leaks_secret_fields`（卧底）/ `test_public_records_never_leak_secret_fields`（阿瓦隆）守这条。
3. **round-robin 铁律 + 无兜底快速失败。** 轮到谁、怎么计票、判胜负全在 `nodes.py`（纯代码）。**AI 调用失败 / 超时（`ModelSpec.timeout`）/ 输出非法（如投了不存在的座位）一律抛错中断本局**，绝不静默替换成占位或随机内容——问题要立刻暴露。无人值守的"崩了重开下一局"应由**外层看护进程**负责（尚未做），不在每步塞兜底。
   **随机分两类、判据是"开打前是否已就位"。** *开局设定的随机*（谁是卧底、用哪对词）只依赖 config+registry、开打前已全部就位 → **前置到边界** `validation.resolve_and_validate` 一次性摇定成 `GameSetup`，连同 cast/models 一起入图，图里拿到的是确定值（并写进 replay 的 `meta.json.setup` 供复盘）。*对局过程的随机* 依赖运行期才有的状态 → 留在节点：当前**唯一一个**是平票随机淘汰（`tally_node`，依赖本轮票数），那是 v1 约定的游戏规则，不是错误兜底。`Deps.rng` 同一实例既喂边界摇牌、也喂 tally。阿瓦隆更干净：对局过程**零裁判侧随机**（表决平票=否决是确定规则），rng 只在边界摇开局（角色/首任队长），故 avalon 的 `Deps` 没有 rng 字段。
4. **出向用流、入向用 Deps。** 游戏→外面（**已实现**）：`events.emit` 经 LangGraph 自定义流（`run.py` 用 `astream(stream_mode=["custom","values"])` 消费，**同一份事件扇出两家**：`console.Renderer` 实时打印 + `Recorder` 落 `events.jsonl`）。外面→游戏（**规划中、尚未接线**）：将来观众押注/场外注入走一个注入进 `Deps` 的 `Interaction.collect(window)` 抽象（收 **0..N** 个输入，生成器里用立即返回空的 `SilentInteraction`——真人弹幕押注本质只能发生在回放/直播当下，由将来 `live/` 的 `DouyinInteraction` 在回放层收）。**当前 `Deps` 尚无 `interaction` 字段、图里也无押注节点。刻意不用** LangGraph `interrupt`（太重、偏离散 resume）。
5. **可替换后端都做成注入的 Deps。** `--player llm|random` 换的是 `Deps.player`，换后端不动图（将来的 `Interaction` 同此模式，尚未接线）。`random` 后端 = 离线 mock，无需任何 key 即可跑通/测试。**两玩法实现这条的位置不同**：*谁是卧底*用两个并列的 Player 类（`LLMPlayer` / 独立的 `RandomPlayer`），random 从决策方法入口就岔开、不走解析落盘；*阿瓦隆*把可替换层**下沉到最底层的模型调用**——只有一个 `LLMPlayer`，注入 `LiveBackend`（真发请求）或 `FakeBackend`（按 `PlayerView` 合法约束现编自由文本/JSON），离线时**照样走完整链路**（拼 system/线程/提问 → extractor 抽取 → parse 校验 → 落盘），故调试走到的代码路径与生产一致。新写玩法优先照阿瓦隆这种"单 Player + 注入后端"的形态。
6. **人设、流派、模型三轴彻底解耦（两玩法同构，各用自己的 data/）。** 一个座位 = `Persona`（cast.yaml，嗓音：只管怎么说话）× `Strategy`（strategies.yaml，赢法哲学：只管风险偏好与主动性）× `ModelSpec`（models.yaml，只有接入信息），运行时用 `settings.cast`/`settings.strategies`/`settings.models` 三套独立座位映射配起来。三层 system prompt = 人设 + 流派 + 公共基本功（`prompts.RULES`）。纪律：人设绝不写能力削弱（嗓音≠智商），流派必须是自洽的强打法（流派≠段位）、缺省全员 `standard`；发给模型的文本句中绝不折行（句中 \n 会把句子撕开发给模型）。
7. **游戏之间不互相 import。** `games/` 下一个文件夹一个玩法、各自自包含（连数据都在自己的 `data/`）。`cli.py` 是组合根，允许 import 各玩法做路由——这不算游戏间耦合。要复用再提取，别提前抽 `core`。
8. **`settings` 即 CLI，配置唯一真源。** `undercover/settings.py` 的 `UndercoverSettings`（`pydantic-settings` 的 `BaseSettings`）**字段即命令行参数**——由 `CliSettingsSource` **自动派生 flag**（加字段=多 flag，**绝不手写 argparse**），无独立 `cli.py`。来源优先级：**直接构造 kwargs > CLI `--xxx` > env `BRAWL_UNDERCOVER_*` > `--config`(JSON) > 默认**（`settings_customise_sources` 定义；`run.main` 只剥离 `--config` 这个元参数，其余 argv 全交给 settings 的 CLI source）。顶层 `cli.py` 只路由子命令、不认识任何 flag。**密钥与游戏配置分两条路**：API key 走 `games/.env`（`settings.py` 启动时 `load_dotenv` 加载，真实环境变量优先），`ModelSpec.api_key_env` 指向变量名——key 不进命令行、不进配置文件、不入库（`.gitignore` 挡 `.env`、留 `.env.example`）。

9. **`--replay <dir>` 落一局完整快照，不给则只渲染不落盘。** 给了就把这局**平铺**写进该目录三份文件：`events.jsonl`（god-view 事件时间线，**驱动回放/画面**，与 `console.Renderer` 同源）+ `llm_calls.jsonl`（每次模型调用一行：messages/输出/token 用量/延时/`ok`/解析结果，**成功与失败都记**，落盘点在 `LLMPlayer` 的唯一收口；卧底的 `RandomPlayer` 无调用故留空文件，阿瓦隆的 `FakeBackend` 走完整链路、照常落盘）+ `meta.json`（配置快照 + 起始时间）。**目录由人手动指定、不嵌时间戳子目录**；**已存在即报错**（抛 `SettingsError`、干净提示无 traceback，**不覆盖、无 `--force`**——要重跑换个名，免得盖掉想留的好局）。不给 `--replay` 时 `Recorder` 为 None、零开销。

## 一局的流程与状态机（谁是卧底）

`undercover/graph.py`：`START → pregame → describe → vote → tally → check →（未结束回 describe / 已结束 postgame）→ END`。

- **pregame**：纯发布节点（与 postgame 对称、`return {}`）——身份与词不在这里决定，已由边界 `validation.resolve_and_validate` 摇定并填进初始 `Seat`，这里只把"对局构成 + 全部底牌"亮给观众（god-view）。
- **describe**：round-robin，每位存活者一句描述 + 独白。后说的人能看到本轮先说者的公开发言。
- **vote**：round-robin，每位投一人 + 理由；投了非法目标/调用失败直接抛错中断（不兜底）。
- **tally**：计票出局，平票由裁判随机定，揭晓出局者身份。
- **check**：按存活构成判胜负（支持任意卧底数）——卧底全灭→平民胜；存活卧底数≥存活平民数→卧底胜；否则继续。对 1 卧底精确退化成"仅剩 2 人卧底胜"。
- **postgame**：揭晓全部身份与胜负。

胜负规则、平票处理可配。生成器**无阶段等待**（瞬间跑完）；唯一的时限是发言者的 LLM 调用超时 `ModelSpec.timeout`（每模型可配，超时直接抛错、不兜底）。细节以 `nodes.py` / `settings.py` 为准。

**押注窗口（开局主押 / 轮押）属规划中——当前图里没有对应节点。** 它依赖"外面→游戏"的输入通道（见不变量 4），而那条通道尚未接线，故先不在状态机里。

## 一局的流程与状态机（阿瓦隆）

`avalon/graph.py`：`START → pregame → propose（队长提队）→ discuss（队长下家起 round-robin）→ team_vote（全员同时亮票，过半通过、平票=否决）→ 通过则 quest（队员同时暗出成功/破坏）→ check → 回 propose / assassinate / postgame → END`。

- **单 graph，角色是数据不是行为主体**：角色差异 =（边界算好的）夜晚情报 + 动作合法性（只有队长提队、蓝方只能出成功、只有刺客刺杀），全是 state/validation 层的事；五种决策走同一个注入的 `Player`（propose/discuss/vote_team/play_quest/assassinate）。
- **同时行动纪律**（卧底没有的新语义）：表决与出牌按座位顺序逐个调用，但视图不含他人本轮的选择；开牌后表决才随 `ProposalRecord` 进公开账本，暗牌永远只公开张数。
- **终局三路**：红方破坏满 3 任务 / 同一任务 5 连否决 → 红胜；蓝方满 3 任务后进刺杀——命中梅林红方翻盘，落空蓝胜（蓝方唯一赢法是挺过刺杀）。
- **角色面**：梅林+刺客必上；派西维尔/莫甘娜/莫德雷德/奥伯伦是 settings 布尔开关（占本阵营名额，余者忠臣/爪牙补满；莫甘娜需派西维尔在场）。夜晚可见性是 `rules.sees` 矩阵（梅林看红方除莫德雷德、派西维尔看梅林+莫甘娜、红方互认除奥伯伦），`night_intel` 在边界派生进 Seat。
- **湖中仙女**（`--lady`，流程模块非角色牌）：第 2/3/4 个任务结束后插一个 `lady` 节点——持有者查验一人真实阵营（私密结果只进该次 `lady_announce` 视图与 god-view 事件）、当众宣布（可撒谎、进 transcript）、令牌传给被查者（历任持有者不可再被查）。初始持有者=首任队长上家，边界摇定进 `GameSetup.lady_holder`（None=未启用）。
- 队长逐提案轮转；唯一裁判随机是边界摇的开局（角色/首任队长/仙女初持者），对局过程零随机。刺杀阶段按官方规则红方终局互认：刺客视图临时拿到完整红方名单（含奥伯伦），指认范围恰为全部蓝方。
- **主持人（host.Host）**：显式主持人负责全部**控场串场**，台词经 `deps.host.announce(threads, beat, text, quest=…, attempt=…)` 一次完成"进全员 inbox + emit host 事件"，与玩家线程一字不差（同源）。**主持词是逐句 TTS 的口播文案**：像真主持人——请队长提队、宣布队伍人选/表决与任务结果/令牌去向/终局揭晓，**绝不复述玩家发言**、不带【】/✔✘/×N 这类念不出的符号（阵容说"四名忠臣"）。玩家发言原文以 `prompts.line_player`（"6号：「…」"）经 `nodes._broadcast` 只进线程（发言已在结构化事件里）；开票票型明细 `line_votes` 同此（公开账本，玩家必须看到）。host 事件契约 `{type: "host", beat, call_id, text}`：beat ∈ opening/deal/propose_intro/team/vote_result/quest_result/lady/assassination/reveal/closing，call_id 确定性命名 `{quest}.{attempt}.host.{beat}`（同拍重复补 .2 起序号），供回放层分样式与离线 TTS 对齐。台词库是 `prompts.line_host_*`（纯代码合成，只说公开信息）；结构化事件照常 emit——数据归数据，台词归台词。主持人 persona 经 `--host-cast` 指定（缺省 herald，cast.yaml 末尾的主持人人设只需 id/name，system 对其不生效），照旧走边界校验并写进 meta.json.setup.host_cast，当前只供显示名与将来 TTS 音色映射。
- **提示词取向（与卧底刻意相反）**：阿瓦隆的 RULES/角色任务书只给规则与事实、不写打法心法秘籍；人设/流派当前为中性极简（cast 只留名字、strategies 只有 standard 一条）——先观察模型只知规则时的原生表现，要不要风格化等真局复盘后再定。

## 测试

**测试随玩法自包含**（不变量 #7）：每个玩法把自己的 pytest 套件放在 `<玩法>/tests/`（当前
`undercover/tests/`），不放仓库顶层；`pyproject.toml` 的 `testpaths` 逐玩法登记。跨玩法的东西
（如 `cli.py` 路由）将来才需要顶层 `games/tests/`。**全套零 key、零网络**——`--player random`
（离线 mock）或纯函数即可，CI 不依赖任何 provider。`cd games && uv run pytest -q`。

按"守哪条不变量"组织，不追行覆盖率：

- `test_undercover_flow.py`：random 后端真跑整局，验**必终局 + winner 合法**（#3）、`Utterance` 永不含 `role/word/monologue`（#2 独白只给观众）。
- `test_nodes.py`：`check_node` 胜负三分支（含多卧底）、`tally_node` 平票随机、`vote_node` 投非法/投自己即抛（#3 无兜底）。直接调节点时 `nodes.emit` 需打桩——图运行上下文外 `get_stream_writer()` 会抛。
- `test_validation.py`：边界 `resolve_and_validate`——缺省补齐、身份/词摇定且 seed 可复现、未知人设/模型、`--replay` 已存在、llm 缺 key 都在开打前炸（#3 前置随机 + fail-fast）。
- `test_settings.py`：字段形状校验（人数自洽、cast/models 长度）+ 来源优先级 kwargs>CLI>env（#8）。
- `test_parsing.py`：从思考前缀/代码围栏/尾巴文字/字符串内花括号里稳健抽 JSON，缺字段/无 JSON 抛错。
- `test_recorder.py`：`DirRecorder` 写三件套 + 拒覆盖、`NullRecorder` 全空操作、`open_recorder` 工厂（#9 + 空对象模式）。

`avalon/tests/` 分三个强度档（"穷举得了的就穷举"）：

- **真穷举**（输入空间有限的纯规则/纯节点函数）：`test_rules.py`（sees 可见性 8×8=64 组合对照独立抄录的期望表、
  fails_required 6 人数档×5 任务全查）、`test_nodes.py` 的 2^5 票型全枚举与破坏票 0..k 门槛全枚举、
  `test_validation.py` 的 6 人数档×16 开关组合角色池全枚举。
- **全轨迹断言**（整局状态空间爆炸、改为 100% 检查实际发生的每一步）：`test_probe.py` 探针 Player 记录每次
  PlayerView，逐条对照"应得信息"预言——情报=night_intel、同时行动零泄漏、开票后票型必达、当前提案不提前进账本。
- **常规**：`test_avalon_flow.py`（必终局+胜负原因自洽+公开记录 schema 零泄漏+replay 三件套）、`test_host.py`
  （host 事件覆盖各拍、与线程同源一字不差、call_id 确定性可复现、台词零泄漏）、`test_nodes.py`
  其余非法输入即抛、`test_settings.py`、`test_parsing.py`、`test_prompts.py`（8 角色×5 决策渲染冒烟）。

两玩法测试文件同名（test_nodes.py 等），pyproject 以 `--import-mode=importlib` 按路径区分模块。

## 跑起来

```bash
cd games && uv sync
# 离线随机后端，无需 key，瞬间跑完
uv run brawl undercover --player random
# 把这局冻成 replay 快照（目录须不存在；含 events.jsonl / llm_calls.jsonl / meta.json）
uv run brawl undercover --player random --replay /tmp/game1
# 真模型：先把 key 填进 games/.env（默认只需 DEEPSEEK_API_KEY），再
uv run brawl undercover --player llm --replay /tmp/game2
# 阿瓦隆（5~10 人；--player random 零 key，特殊角色与湖中仙女按开关）
uv run brawl avalon --player random --players 10 --percival --morgana --mordred --oberon --lady --replay /tmp/game3
uv run pytest -q
```

## 阅读顺序

以下以 undercover 为例；avalon 逐文件同构（多一个 `rules.py`：规则常数表+夜晚可见性矩阵），按同样顺序通读 `avalon/` 即可。

1. 先读本 SKILL.md（地图 + 不变量）。
2. 想懂一局怎么走：`undercover/graph.py` → `nodes.py`。
3. 数据结构与不变量落点：`undercover/state.py`。
4. 换/加决策后端：`undercover/player.py`（+ `deps.py`）。
5. 开局方案怎么解析+摇定：`undercover/validation.py`（边界，产出 `GameSetup`）。（押注/外部输入通道 `interaction.py` 规划中、尚未创建。）
6. 配置与 CLI：`undercover/settings.py`（真源，本身即 CLI、自动派生 flag）+ `games/cli.py`（子命令路由）。回放产物落盘：`undercover/recorder.py`。
7. 人设/流派/模型/词：`undercover/data/*.yaml` + `registry.py`。
8. 渲染/事件契约：`undercover/console.py` + `events.py`（也是将来 stage/live 的接入面）。
9. 不变量怎么被守住：`undercover/tests/`（每个文件对一条不变量，见上「测试」）。
10. 动手前对照"核心不变量"。
