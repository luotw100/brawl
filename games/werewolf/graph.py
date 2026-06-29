"""狼人杀 LangGraph 装配：把裁判节点连成昼夜循环的状态机。

START → opening → night_start → wolf_kill → seer → witch → resolve_night → dawn → last_words
                      ↑                                                                 │
                      │                                        ┌──route_after_hunter────┤
                      │                                        ↓                        │
                      │                                      hunter ──────────────→ check
                      │                                                                 │
        ┌─────────────┘←──(night)── route_after_check ──(day)──→ discussion → vote → resolve_vote
        │                                  │                                              │
   night_start                          (end)                                            └→ last_words
                                           ↓                                            （复用同一个 last_words → hunter → check）
                                        postgame → END

last_words / hunter / check 被黑夜与白天两条支路复用，靠 state.phase 区分：resolve_night 置
after_night（check 后走白天），resolve_vote 置 after_vote（check 后走下一夜并令 day+1）。
死者留遗言在前、死亡的猎人开枪在后、判胜负最后——枪在结算之前、天亮之后。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from werewolf import nodes
from werewolf.state import GameState


def build_graph(checkpointer: Any = None):
    """装配并编译图。给了 checkpointer（续跑用）就让 LangGraph 每个 super-step 后持久化整份
    GameState；不给（缺省）则纯内存跑、不可续。"""
    g = StateGraph(GameState)
    g.add_node("opening", nodes.opening_node)
    g.add_node("night_start", nodes.night_start_node)
    g.add_node("wolf_kill", nodes.wolf_kill_node)
    g.add_node("seer", nodes.seer_node)
    g.add_node("witch", nodes.witch_node)
    g.add_node("resolve_night", nodes.resolve_night_node)
    g.add_node("dawn", nodes.dawn_node)
    g.add_node("last_words", nodes.last_words_node)
    g.add_node("hunter", nodes.hunter_node)
    g.add_node("check", nodes.check_win_node)
    g.add_node("discussion", nodes.discussion_node)
    g.add_node("vote", nodes.vote_node)
    g.add_node("resolve_vote", nodes.resolve_vote_node)
    g.add_node("postgame", nodes.postgame_node)

    g.add_edge(START, "opening")
    g.add_edge("opening", "night_start")
    g.add_edge("night_start", "wolf_kill")
    g.add_edge("wolf_kill", "seer")
    g.add_edge("seer", "witch")
    g.add_edge("witch", "resolve_night")
    g.add_edge("resolve_night", "dawn")
    g.add_edge("dawn", "last_words")
    # 遗言后：有待开枪的猎人先开枪，否则直接判胜负。猎人开枪后回 route 必然走 check（pending 已清）。
    g.add_conditional_edges("last_words", nodes.route_after_hunter, {"hunter": "hunter", "check": "check"})
    g.add_conditional_edges("hunter", nodes.route_after_hunter, {"hunter": "hunter", "check": "check"})
    g.add_conditional_edges("check", nodes.route_after_check, {"end": "postgame", "day": "discussion", "night": "night_start"})
    g.add_edge("discussion", "vote")
    g.add_edge("vote", "resolve_vote")
    g.add_edge("resolve_vote", "last_words")
    g.add_edge("postgame", END)
    return g.compile(checkpointer=checkpointer)
