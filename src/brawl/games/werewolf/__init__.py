"""狼人杀：自包含玩法包（Sentence Runtime 版）。

入口 run.main；CLI 经 brawl/cli.py 的 `brawl werewolf` 子命令进来。
语义全在 rules_engine.WerewolfRules；brawl.runtime 只负责驱动循环与句子日志。
"""

