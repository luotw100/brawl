# brawl-games

AI 直播对战的纯游戏逻辑层。一个文件夹一个玩法，各自自包含、互不 import。当前实现：谁是卧底、阿瓦隆。

## 跑一局

```bash
uv sync
# 随机后端，无需任何 API key，瞬间跑完（生成器无 wall-clock，不计时不等待）
uv run brawl undercover --player random
# 把这局冻成 replay 快照（目录须不存在）
uv run brawl undercover --player random --replay /tmp/game1

# 真模型在玩（先把 key 填进 games/.env；模型池见各玩法 data/models.yaml）
uv run brawl undercover --player llm --players 6 --undercover 1

# 阿瓦隆：生成 replay 后，可继续在同一目录内生成发布素材
uv run brawl avalon --player random --players 10 --percival --morgana --mordred --oberon --lady --replay /tmp/avalon1
uv run brawl avalon-tts /tmp/avalon1
uv run brawl avalon-title /tmp/avalon1
```

## 测试

```bash
uv run pytest -q   # 全套零 key、零网络（--player random / 纯函数）
```

## 配置优先级

直接构造 kwargs > CLI `--xxx` > 环境变量（前缀 `BRAWL_UNDERCOVER_`）> `--config xxx.json` > 默认。字段见 `undercover/settings.py`（settings 即 CLI，flag 自动派生）。

## 架构地图

见 `../.agents/skills/repository-overview/SKILL.md`。
