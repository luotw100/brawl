"""阿瓦隆：自包含玩法包。

入口 run.run_game(settings)；CLI 经 games/cli.py 的 `brawl avalon` 子命令进来。
架构地图见 .agents/skills/repository-overview/SKILL.md。

当前为 random / opencode 双后端，人设/流派/模型三轴配置已就位；
agent 调用链路在 core.actor，Avalon 专属解析在 player.py / prompts.py。
"""
