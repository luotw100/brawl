"""句子运行时（Sentence Runtime）：丢弃 LangGraph 后的 core 引擎。

子模块：
- types：游戏盲类型（Delivery / Sentence / Expectation / ParseResult）
- rules：玩法插件 Protocol
- journal：句子日志 = checkpoint（替代旧 recorder + resume sidecar）
- loop：驱动循环 + resume
- dummy：最小参考实现 + 回归测试夹具

语义全在 rules，管道全在 core。本包搜不到任何玩法名词。
"""

from __future__ import annotations
