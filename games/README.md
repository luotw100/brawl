# brawl-games

AI 直播对战的纯游戏逻辑层。`core/` 提供跨玩法运行时，`avalon/` 保留阿瓦隆玩法规则、状态机和提示词。

## 环境初始化

```bash
# 默认轻量环境：不安装 Qwen3-TTS / PyTorch。
uv sync
```

如需本地 Qwen3-TTS runtime，每台机器初始化一次即可。CPU 机器：

```bash
uv sync --extra qwen3-tts-cpu
```

NVIDIA CUDA 12.8 机器：

```bash
uv sync --extra qwen3-tts-cu128
```

初始化完成后，日常仍然直接 `uv run brawl ...`，不需要每次给 `uv run` 追加 `--extra`。
如果要把当前 checkout 当成一台新机器重新初始化，删除本项目虚拟环境后重新 sync：

```bash
rm -rf .venv
uv sync --extra qwen3-tts-cpu     # 或 qwen3-tts-cu128
```

不要在同一个 `.venv` 里反复切换 CPU/CUDA extra；切换前先删 `.venv`。

## 跑一局

```bash
# 阿瓦隆：生成 replay 后，可继续在同一目录内生成发布素材
uv run brawl avalon --player random --players 10 --percival --morgana --mordred --oberon --lady --replay /tmp/avalon1
uv run brawl avalon-tts /tmp/avalon1
```

## 测试

```bash
uv run pytest -q   # 全套零 key、零网络（--player random / 纯函数）
```

## 配置优先级

直接构造 kwargs > CLI `--xxx` > 环境变量（前缀 `BRAWL_AVALON_`）> `--config xxx.json` > 默认。字段见 `avalon/settings.py`（settings 即 CLI，flag 自动派生）。

## 框架分布

- `core/actor.py`：通用 LLM 调用、重试、extractor 解析、llm_calls 落盘。
- `core/registry.py` / `setup.py`：模型/人设/流派/主持人注册表，以及通用边界解析。
- `core/threads.py` / `host.py`：每席位私有线程、公开广播、主持人口播事件。
- `core/recorder.py`：replay 目录写入与空对象 recorder。
- `core/resume.py`：checkpoint 数据库路径、sidecar 水位和续跑截断点。
- `core/runner.py`：LangGraph 事件流驱动与 checkpoint 水位对齐。
- `core/parsing.py` / `events.py` / `models.py`：JSON 抽取、事件 ID/emit、模型规格。

## 架构地图

见 `../.agents/skills/repository-overview/SKILL.md`。
