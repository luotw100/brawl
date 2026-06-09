"""主持人（Host）：把原先散在各节点的公共叙述扶正为显式角色。

Host 不是 Player——没有视角、不决策、零 LLM 调用、零 wall-clock；台词由裁判节点用
prompts.line_* 纯代码合成。announce 一次完成两件事，保证玩家线程里的公共叙述与
host 事件文本一字不差（同源）：

  1. 广播进全员 inbox（玩家视角 = 别人的发言 + 主持人的话，拼 prompt 零脏逻辑）；
  2. emit("host", beat=..., call_id=..., text=...) 进 god-view 事件流——回放层按 beat
     分样式、触发过场，call_id 供将来离线 TTS 按它对齐音频文件。

结构化事件（vote_result/quest_result/...）照常由节点 emit、驱动回放层的 UI 状态；
host 事件只负责"说出来的话"——数据归数据，台词归台词。Host 只说公开信息：台词本来
就进全员线程，结构上零泄漏。

persona（--host-cast，经边界校验写进 meta.json.setup.host_cast）只决定显示名与将来
TTS 的音色映射，运行期 Host 用不到，故这里不持有它。
"""

from __future__ import annotations

from avalon.events import emit
from avalon.state import SeatThread


class Host:
    """call_id 确定性命名（仿 events.decision_id）：{quest}.{attempt}.host.{beat}；
    同一拍在同一 (quest, attempt) 内多次出现（如 discuss/banter 逐人一条）时从第二条起
    补序号 .2、.3……整局唯一，同 seed 同一局可逐字复现。"""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def announce(self, threads: dict[int, SeatThread], beat: str, text: str, *, quest: int, attempt: int) -> None:
        base = f"{quest}.{attempt}.host.{beat}"
        n = self._seen[base] = self._seen.get(base, 0) + 1
        for t in threads.values():
            t.inbox.append(text)
        emit("host", beat=beat, call_id=base if n == 1 else f"{base}.{n}", text=text)
