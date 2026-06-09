"""LangGraph 装配：把裁判节点连成一局的状态机。

START → pregame → 描述 → 投票 → 计票 → 判胜负
                   ↑_________________|（未结束）
                                     └→ postgame → END（已结束）
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from undercover import nodes
from undercover.state import GameState


def build_graph():
    g = StateGraph(GameState)
    g.add_node("pregame", nodes.pregame_node)
    g.add_node("describe", nodes.describe_node)
    g.add_node("vote", nodes.vote_node)
    g.add_node("tally", nodes.tally_node)
    g.add_node("check", nodes.check_node)
    g.add_node("postgame", nodes.postgame_node)

    g.add_edge(START, "pregame")
    g.add_edge("pregame", "describe")
    g.add_edge("describe", "vote")
    g.add_edge("vote", "tally")
    g.add_edge("tally", "check")
    g.add_conditional_edges("check", nodes.route_after_check, {"continue": "describe", "end": "postgame"})
    g.add_edge("postgame", END)
    return g.compile()
