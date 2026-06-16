"""音效 cue 表：一拍 → 该放哪些音效（名字 + 拍内偏移秒）。

这是直播页（web/src/stage/sfx.ts 的 cuesFor）与离线录制（record.py 的 mux）共用的同一套
映射。跨语言没法共享代码，所以两边必须手动保持一致——改这里就同步改 sfx.ts。

音效文件在 web/public/sfx/<name>.opus（构建后落到 web_dist/sfx/）。名字即契约：换素材只换
同名文件，这张表不动。
"""

from __future__ import annotations

from server.protocol import (
    Assassination,
    Deal,
    Directive,
    GameOpen,
    GameOver,
    QuestReveal,
    Speech,
    VoteReveal,
)

SFX_VOLUME = 0.7  # 音效统一压在人声之下（直播页 el.volume / mux 里 volume 滤镜同此值）


def cues_for(directive: Directive, duration: float) -> list[tuple[str, float]]:
    """一拍 → [(音效名, 拍内偏移秒)]。无音效的拍（主持/发言无内心戏/幕间）返回空。"""
    match directive:
        case GameOpen():
            return [("open", 0.0)]
        case Deal():
            return [("deal", 0.0)]
        case Speech():
            return [("think", 0.0)] if directive.monologue else []
        case VoteReveal():
            # 逐票"啪"盖章 + 结果音；错峰节奏 = duration*0.62/n，与 Stage.tsx 的 --vote-stagger 同步
            n = len(directive.votes)
            stagger = duration * 0.62 / max(n, 1)
            cues = [("stamp", i * stagger) for i in range(n)]
            cues.append(("vote-pass" if directive.approved else "vote-fail", n * stagger))
            return cues
        case QuestReveal():
            return [("quest-success" if directive.result == "success" else "quest-fail", 0.0)]
        case Assassination():
            return [("assassin-hit" if directive.hit else "assassin-miss", 0.0)]
        case GameOver():
            return [("win-good" if directive.winner == "good" else "win-evil", 0.0)]
        case _:
            return []
