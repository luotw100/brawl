"""阿瓦隆：自包含玩法包。

入口 run.run_game(settings)；CLI 经 games/cli.py 的 `brawl avalon` 子命令进来。
架构地图见 .agents/skills/repository-overview/SKILL.md。

当前为 random 后端 MVP（跑通全流程 + 测试），人设/流派/模型三轴配置已就位；
LLM 后端（player.py 的 LLMPlayer、prompts.py、parsing.py）待授权后再加。
"""
