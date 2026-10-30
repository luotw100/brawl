# brawl

让 Agent 主持不同模型参与的阿瓦隆对局，产出可复盘的文本内容。

当前对话中的 Agent 担任主持者：读取文字规则、协调各座位模型、裁决行动并记录局面。
各座位通过 API 独立决策，对话历史保存在各自目录中。通用工具负责模型通信和持久化。
主持者解释行动、裁决合法性并决定阶段；Python 承担随机、文件操作和已确定数据的计算。

板子为**阿瓦隆 10 人全角色，带湖中仙女**。
好人是梅林、派西维尔、4 忠臣；坏人是刺客、莫甘娜、莫德雷德、奥伯伦。

## 使用

在这个仓库打开 Agent 对话，例如：

> 开一局阿瓦隆 10 人全角色湖仙局，所有座位用 deepseek-flash，存到 runs/avalon-demo，开始主持。

明确游戏配置、座位模型和存档位置后，主持者直接执行。
有缺项时，主持者根据仓库规则、已有对局配置和可用模型整理选项，一次性请你选择确认。

续局时：

> 根据 runs/某一局/state.md 和已有存档继续主持这局。

`AGENTS.md` 指向 [主持 skill](skills/brawl-play/SKILL.md) 和 [板子规则](skills/brawl-play/rules/avalon.md)。
Skill 保存在仓库的 `skills/` 目录，通过 AGENTS.md 或显式文件路径加载。
具体操作见 [一次行动、纠错、快照与异常示例](skills/brawl-play/references/hosting.md)。
对局期间严禁新增或修改程序代码，严格按 skill 使用现有工具；不顺手之处在终局后统一提出修改建议。随机和一次性数据操作按 skill 的既定约定执行。

## 模型调用工具

运行环境为 Python 3.12 标准库。凭证放仓库根 `.env`，可以参照 `.env.example`；已有环境变量优先。
工具的默认模型为 `deepseek-flash`，开局模型按用户选择确定；可用模型通过 `models` 命令查询。

```bash
python tools/seat_chat.py models
python tools/seat_chat.py init --game runs/demo --seat 01 \
  --system-file runs/demo/outbox/01-system.md --model deepseek-flash \
  --parameters '{"max_tokens":8192,"thinking":{"type":"enabled"}}'
python tools/seat_chat.py send --game runs/demo --seats 01 --file runs/demo/outbox/turn-01.md
python tools/seat_chat.py call --game runs/demo --seat 01
```

`--system-file` 是主持者准备的规则、身份和人设；`send --file` 是新增可见事件与当前行动要求。
同一份公开通知可通过 `--seats 01 02 03` 投递给多个座位，私密通知只投递给相应座位。
这两个文件使用 UTF-8 文本，由主持者组织内容。

`init` 支持 `--base-url`、`--api-key-env`、`--model` 和厂商参数 `--parameters`，用于其他兼容 Chat Completions 的服务。
`call` 自动读取完整座位历史，向主持者返回本轮答复、usage 和审计路径。
选手 API 接收该座位的完整历史，主持者根据当前回复和局面推进对局。

## 存档

```text
runs/时间-avalon/
  rules.md                 本局规则
  roster.md                模型、角色、人设和初始私密情报
  random.md                随机种子、候选输入、执行脚本和结果
  state.md                 当前局面、待处理 ID 和下一步的短快照
  replay.md                完整记录：公开、私密、裁决、修正
  summary.md               结果、转折和复盘索引
  outbox/                  主持者准备的投递文本
  seats/01/
    system.md              座位设定
    config.json            模型连接参数与凭证变量名
    history.jsonl          每行一组完整 user/assistant 往返
    inbox/                 持久化增量通知，以历史中的 ID 判断是否已消费
    calls/调用ID/          原始请求、响应、输入 ID、提交状态
```

对局正文使用自由组织的 Markdown。座位的 JSON/JSONL 用于保存 API 通信记录。
完整事件和裁决保存在 `replay.md`，原始回复保存在座位档案；`state.md` 通过事件和调用 ID 引用这些记录。
每次调用归档请求正文、原始响应和提交状态，凭证在发送时从本机加载。
完整文字回复提交到座位历史；调用异常或输出不完整时，保留待处理输入与调用记录，由主持者查看状态后决定下一步。
已收到完整响应、尚未提交历史时，用 `call --recover <调用ID>` 将保存的响应提交到历史。

主持者纠正规则误解、明确的记忆错误与行动矛盾，保留合法但愚蠢的决策，并按游戏规则处理欺骗与伪装。
信息范围由提示词和主持者的消息分发共同约束。
发牌、起始队长和抽签通过实际执行 Python `random` 完成，种子取执行时的 `int(time.time())`（Unix 时间戳，精确到秒），种子与结果随局保存。

## 验证

```bash
python -m unittest discover -s tools/tests -v
```

测试使用本地模拟响应，覆盖座位历史续接、定向投递、调用失败、截断回复和中断恢复。
主持准确性与对局内容质量通过真实阿瓦隆比赛继续打磨。
