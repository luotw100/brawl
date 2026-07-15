"""阿瓦隆：自包含玩法包（Sentence Runtime 版）。

入口 run.main；CLI 经 games/cli.py 的 `brawl avalon` 子命令进来。
语义全在 rules_engine.AvalonRules；core.sr 只负责驱动循环与句子日志。
"""
