"""LangGraph 装配：把裁判节点连成一局的状态机。

START → pregame → 组队 → 讨论 → 表决 ──通过──→ 任务 → 查任务轨 ──未分胜负──→ 组队
                    ↑                │                    │          （第 2/3/4 个任务后
                    │←──否决(<5 次)──┘                    │            先经湖中仙女查验）
                    │                                     ├─ 蓝方满 3 任务 → 刺杀 ─┐
                    │                                     └─ 红方满 3 任务 ────────┤
                    └（第 5 次否决：红方胜）──────────────────────→ postgame → END
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from avalon import nodes
from avalon.state import GameState


def build_graph(checkpointer: Any = None):
    """装配并编译图。给了 checkpointer（续跑用，见 resume.py）就让 LangGraph 在每个
    super-step 后持久化整份 GameState；不给（缺省，等价旧行为）则纯内存跑、不可续。"""
    g = StateGraph(GameState)
    g.add_node("pregame", nodes.pregame_node)
    g.add_node("propose", nodes.propose_node)
    g.add_node("discuss", nodes.discuss_node)
    g.add_node("team_vote", nodes.team_vote_node)
    g.add_node("quest", nodes.quest_node)
    g.add_node("check", nodes.check_node)
    g.add_node("lady", nodes.lady_node)
    g.add_node("assassinate", nodes.assassinate_node)
    g.add_node("postgame", nodes.postgame_node)

    g.add_edge(START, "pregame")
    g.add_edge("pregame", "propose")
    g.add_edge("propose", "discuss")
    g.add_edge("discuss", "team_vote")
    g.add_conditional_edges("team_vote", nodes.route_after_vote, {"quest": "quest", "repropose": "propose", "end": "postgame"})
    g.add_edge("quest", "check")
    g.add_conditional_edges(
        "check", nodes.route_after_check,
        {"continue": "propose", "lady": "lady", "assassinate": "assassinate", "end": "postgame"},
    )
    g.add_edge("lady", "propose")
    g.add_edge("assassinate", "postgame")
    g.add_edge("postgame", END)
    return g.compile(checkpointer=checkpointer)
