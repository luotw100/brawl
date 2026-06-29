"""狼人杀 LangGraph 装配。"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from werewolf import nodes
from werewolf.state import GameState


def build_graph(checkpointer: Any = None):
    g = StateGraph(GameState)
    g.add_node("opening", nodes.opening_node)
    g.add_node("night_start", nodes.night_start_node)
    g.add_node("wolf_kill", nodes.wolf_kill_node)
    g.add_node("seer", nodes.seer_node)
    g.add_node("witch", nodes.witch_node)
    g.add_node("resolve_night", nodes.resolve_night_node)
    g.add_node("day_announce", nodes.day_announce_node)
    g.add_node("last_words", nodes.last_words_node)
    g.add_node("discussion", nodes.discussion_node)
    g.add_node("vote", nodes.vote_node)
    g.add_node("resolve_vote", nodes.resolve_vote_node)
    g.add_node("hunter", nodes.hunter_node)
    g.add_node("check", nodes.check_win_node)
    g.add_node("postgame", nodes.postgame_node)

    g.add_edge(START, "opening")
    g.add_edge("opening", "night_start")
    g.add_edge("night_start", "wolf_kill")
    g.add_edge("wolf_kill", "seer")
    g.add_edge("seer", "witch")
    g.add_edge("witch", "resolve_night")
    g.add_conditional_edges("resolve_night", nodes.route_after_hunter, {"hunter": "hunter", "check": "check"})
    g.add_conditional_edges("hunter", nodes.route_after_hunter, {"hunter": "hunter", "check": "check"})
    g.add_conditional_edges("check", nodes.route_after_check, {"end": "postgame", "day": "day_announce", "night": "night_start"})
    g.add_edge("day_announce", "last_words")
    g.add_edge("last_words", "discussion")
    g.add_edge("discussion", "vote")
    g.add_edge("vote", "resolve_vote")
    g.add_conditional_edges("resolve_vote", nodes.route_after_hunter, {"hunter": "hunter", "check": "check"})
    g.add_edge("postgame", END)
    return g.compile(checkpointer=checkpointer)

