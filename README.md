# brawl

让 Agent 主持不同模型参与的阿瓦隆对局，产出可复盘的文本内容。

由当前对话里的 Agent 充当 driver：读文字规则、直接调用各座位模型、裁决和记录。
各座位有独立对话历史，无需 Codex subagent。只有模型通信与持久化使用通用小工具，游戏规则不写进代码。

只维护一个板子：**阿瓦隆 10 人全角色，带湖中仙女**。
好人是梅林、派西维尔、4 忠臣；坏人是刺客、莫甘娜、莫德雷德、奥伯伦。

## 使用

在这个仓库打开 Agent 对话，例如：

> 开一局阿瓦隆 10 人全角色湖仙局，所有座位用 DeepSeek，开始主持并保存存档。

或：

> 按 runs/某一局/state.md 接着主持这局，不要重新发牌。

`AGENTS.md` 指向 [主持 skill](skills/brawl-play/SKILL.md) 和 [板子规则](skills/brawl-play/rules/avalon.md)。
Skill 放在普通 `skills/` 目录，通过 AGENTS.md 或显式文件路径加载；不依赖用户机器的全局 skill 安装。

## 模型调用工具

仅需 Python 3.12 标准库。凭证放仓库根 `.env`，可以参照 `.env.example`；已有环境变量优先。
默认模型为 `deepseek-flash`，可用模型通过 API 查询，不在代码里维护厂商模型表。

```bash
python tools/seat_chat.py models
python tools/seat_chat.py init --game runs/demo --seat 01 \
  --system-file /tmp/seat-01.md --model deepseek-flash \
  --parameters '{"max_tokens":8192,"thinking":{"type":"enabled"}}'
python tools/seat_chat.py send --game runs/demo --seats 01 --file /tmp/turn-01.md
python tools/seat_chat.py call --game runs/demo --seat 01
```

`--system-file` 是主持者准备的规则、身份和人设；`send --file` 是新增可见事件与当前行动要求。
同一份公开通知可通过 `--seats 01 02 03` 投递给多个座位，私密通知只投递给相应座位。
这两个文件可以是任意 UTF-8 文本，工具不解析游戏语义。

`init` 支持 `--base-url`、`--api-key-env`、`--model` 和厂商参数 `--parameters`，用于其他兼容 Chat Completions 的服务。
`call` 自动读取完整座位历史，只输出本轮答复、usage 和审计路径，不把完整 prompt 打回主持上下文。
选手 API 本身仍接收完整历史，第一版不自动摘要或裁剪。

## 存档

```text
runs/时间-avalon/
  rules.md                 本局规则
  roster.md                模型、角色、人设和初始私密情报
  state.md                 主持进度及待处理行动
  replay.md                完整记录：公开、私密、裁决、修正
  summary.md               结果、转折和复盘索引
  outbox/                  主持者准备的投递文本
  seats/01/
    system.md              座位设定
    config.json            模型连接参数，不含 key
    history.jsonl          每行一组完整 user/assistant 往返
    inbox/                 持久化增量通知，以历史中的 ID 判断是否已消费
    calls/调用ID/          原始请求、响应、输入 ID、提交状态
```

对局正文自由使用 Markdown，不要求固定事件 schema。座位的 JSON/JSONL 是通信工具内部使用的普通 UTF-8 文本。
每次请求保存完整输入和原始响应，不保存 Authorization header。
网络或 API 错误不会消费 inbox，也不会自动重试；不完整输出保留在调用档案中但不加入有效对话。
已收到完整响应、尚未提交历史时，可用 `call --recover <调用ID>` 恢复，不重复调用模型。

主持者只纠正规则误解、明确的记忆错误与行动矛盾；保留合法但愚蠢的决策，也允许角色正常欺骗。
信息范围靠提示词与主持者分发约束；第一版不增加沙箱或整局预算限制。

## 验证

```bash
python -m unittest discover -s tools/tests -v
```

测试覆盖座位历史续接、定向投递、调用失败、截断回复和中断恢复；不访问网络。
主持准确性与对局内容质量通过真实阿瓦隆比赛继续打磨。
