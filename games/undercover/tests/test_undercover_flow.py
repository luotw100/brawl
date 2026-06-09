"""端到端冒烟 + 跨数据通道的不变量：用 --player random 真跑完整一局（零 key、零网络）。

守两条核心不变量：
- #3 round-robin + 必终局：每轮恰好淘汰一人，随机后端下一局必在有限轮内分出胜负。
- #2 独白只给观众：AI 之间可见的 Utterance 永不携带身份/词/独白。

asyncio_mode=auto（见 pyproject），async 测试函数直接由 pytest-asyncio 跑。
"""

from __future__ import annotations

from undercover.run import run_game
from undercover.settings import UndercoverSettings
from undercover.state import Utterance


async def test_game_finishes_with_a_winner() -> None:
    """随机后端跑一局：必定终局，winner 是两个合法值之一。"""
    final = await run_game(UndercoverSettings(player="random", players=6, undercover=1))
    assert final.finished is True
    assert final.winner in {"civilians", "undercover"}
    assert final.round >= 1


async def test_multi_undercover_win_conditions_hold() -> None:
    """多卧底（2/6）也能正常跑完并给出合法胜负——胜负判定支持任意卧底数。"""
    final = await run_game(UndercoverSettings(player="random", players=6, undercover=2))
    assert final.finished is True
    assert final.winner in {"civilians", "undercover"}


async def test_undercover_eliminated_means_civilians_win() -> None:
    """1 卧底局：一旦终局，存活构成必须自洽——
    平民胜则卧底全灭；卧底胜则存活卧底数 ≥ 存活平民数。"""
    final = await run_game(UndercoverSettings(player="random", players=6, undercover=1))
    alive = final.alive_seats()
    alive_uc = [s for s in alive if s.role.value == "undercover"]
    alive_civ = [s for s in alive if s.role.value == "civilian"]
    if final.winner == "civilians":
        assert alive_uc == []
    else:
        assert len(alive_uc) >= len(alive_civ)


async def test_transcript_never_leaks_secret_fields() -> None:
    """铁律 #2：公开发言记录 Utterance 的 schema 里就不该有秘密字段；
    跑一局后逐条复核，序列化出来的 key 与 {role, word, monologue} 完全不相交。"""
    # schema 层面：Utterance 根本没有这些字段
    assert {"role", "word", "monologue"}.isdisjoint(Utterance.model_fields)

    final = await run_game(UndercoverSettings(player="random", players=6, undercover=1))
    assert final.transcript, "一局至少应产生若干公开发言"
    for utt in final.transcript:
        assert {"role", "word", "monologue"}.isdisjoint(utt.model_dump())
