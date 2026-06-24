"""Player：席位背后的"脑子"。全项目只有一种 Player——LLMPlayer，它把一次决策拆成
"玩家自由文本作答 → 固定轻量 extractor 解析校验"的完整链路；**唯一可替换的是最底层那次
模型调用**（ModelBackend，由 build_player 在边界注入）：

- LiveBackend：真发请求（langchain ChatOpenAI），按 spec 现建/缓存模型，读环境变量里的 key。
- FakeBackend：离线假后端，不联网、不读 key——按 PlayerView 的合法约束现编应答（自由文本/JSON），
  用于 --player random 跑通/调试。除了"模型调用"这一层，system/线程/提问 → 抽取 → parse 校验
  → 落盘的整条流程与真模型**逐字相同**，故调试时走到的代码路径与生产一致。

每个决策方法返回 (结构化结果, 自由文本原文)：原文由节点回灌进该席位线程（append-only），
模型下轮能看到自己之前的想法。Player 只看 PlayerView 里的"该席位当前可见信息"：自己的
角色与夜晚情报（阿瓦隆里**自己的身份自己知道**，这是规则）、自己线程的消息、节点拼好的
本轮提问。绝不接触别人的角色/情报/独白/任务暗牌。
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from avalon import prompts
from avalon.events import decision_id
from avalon.parsing import JsonParseError, parse_json
from avalon.recorder import Recorder
from avalon.registry import ModelSpec
from avalon.state import (
    Assassination,
    LadyChoice,
    Proposal,
    QuestCard,
    Role,
    Side,
    Statement,
    TeamVote,
    ThreadMessage,
)

_ROLE = {SystemMessage: "system", HumanMessage: "human", AIMessage: "assistant"}


@dataclass
class PlayerView:
    """喂给 Player 的、某席位当前能看到的全部信息。座位号一律 1..players。"""

    seat_no: int
    role: Role  # 自己的角色（自己知道）
    intel: list[int]  # 自己的夜晚情报（座位号；含义随角色：梅林=红方名单，红方=同伙名单）
    model: ModelSpec | None  # llm 后端用；random 后端忽略
    lineup: list[Role]  # 本局启用的角色多重集（公开信息：阵容全桌皆知，对位关系才是秘密）
    players: int  # 总人数（座位号 1..players，阿瓦隆没人出局）
    leader_no: int  # 当前队长
    quest_no: int  # 当前任务序号（1..5）
    attempt: int  # 当前任务的第几次组队（1..5）
    team_size: int  # 本任务队伍人数
    fails_required: int  # 本任务几张破坏票算失败
    succeeded: int  # 已成功的任务数（公开任务轨）
    failed: int  # 已失败的任务数
    team: list[int]  # 当前提案的队伍（组队阶段为空：还没提）
    # 湖中仙女（公开部分）：历任持有者（含当前，查验不得选他们）。未启用 = 空。
    lady_used: list[int]
    # 湖中仙女（私密部分，仅 lady_announce 阶段非空）：自己刚查验的对象与其真实阵营。
    lady_target: int | None = None
    lady_seen: str | None = None  # "good" | "evil"
    # 人设腔调文本（registry.Persona.tone，只管怎么说话）：进 system 首段，并在每轮提问尾部
    # 重复一次（风格提醒，抗长局风格塌缩）。网名/name 绝不进 prompt。
    tone: str = ""
    # 流派文本（registry.Strategy.system，只管怎么赢/风险偏好与主动性）：进 system，影响决策。
    strategy: str = ""
    # 该席位的私有线程（human/assistant 交替，append-only）与节点拼好的本轮 human 提问。
    messages: list[ThreadMessage] = field(default_factory=list)
    prompt: str = ""

    def others(self) -> list[int]:
        return [no for no in range(1, self.players + 1) if no != self.seat_no]

    def good_suspects(self) -> list[int]:
        """除自己和已知同伙外的座位——红方视角下"可能是梅林"的指认候选。"""
        return [no for no in self.others() if no not in self.intel]

    def lady_candidates(self) -> list[int]:
        """湖中仙女可查验的对象：从未持有过令牌的人（自己作为现任持有者天然被排除）。"""
        return [no for no in range(1, self.players + 1) if no not in self.lady_used]


class Player(Protocol):
    async def propose(self, view: PlayerView) -> tuple[Proposal, str]: ...
    async def discuss(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def vote_team(self, view: PlayerView) -> tuple[TeamVote, str]: ...
    async def play_quest(self, view: PlayerView) -> tuple[QuestCard, str]: ...
    async def assassinate(self, view: PlayerView) -> tuple[Assassination, str]: ...
    async def lady_choose(self, view: PlayerView) -> tuple[LadyChoice, str]: ...
    async def lady_announce(self, view: PlayerView) -> tuple[Statement, str]: ...
    async def banter(self, view: PlayerView) -> tuple[Statement, str]: ...


# ---------------------------------------------------------------- 模型调用后端（唯一的可替换层）

class ModelBackend(Protocol):
    """一次模型调用的抽象：给定 (阶段, 视图, 模型规格, 消息) 返回一条带 .content 的应答。

    这是 LLMPlayer 链路里**唯一**因"真模型 / 假后端"而异的地方——其余（拼 system/线程/提问、
    抽取、解析校验、落盘）两者逐字相同。phase/view 真后端用不上（它只认 messages），但传进来
    让假后端能按本席位的合法约束现编应答。"""

    async def call(self, phase: str, view: PlayerView, spec: ModelSpec, messages: list[BaseMessage]) -> BaseMessage: ...


class LiveBackend:
    """真模型后端：langchain ChatOpenAI 普通补全。按 spec 现建并缓存模型（含 extractor 与各
    席位的玩家模型，同名复用一个实例）；api_key 在首次用到该 spec 时才读环境变量
    （_check_env 已在边界保证存在）。"""

    def __init__(self) -> None:
        self._models: dict[str, ChatOpenAI] = {}

    def _model(self, spec: ModelSpec) -> ChatOpenAI:
        model = self._models.get(spec.name)
        if model is None:
            model = ChatOpenAI(
                model=spec.model_id,
                base_url=spec.base_url,
                api_key=os.environ[spec.api_key_env],  # 必存在：_check_env 已在边界校验
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                timeout=spec.timeout,
                extra_body=spec.extra_body or None,
            )
            self._models[spec.name] = model
        return model

    async def call(self, phase: str, view: PlayerView, spec: ModelSpec, messages: list[BaseMessage]) -> BaseMessage:
        return await self._model(spec).ainvoke(messages)


class FakeBackend:
    """离线假后端：不联网、不读 key、形状合法。把"模型调用"替换成按 PlayerView 现编的应答——
    converse 阶段吐标签行体例的自由文本（组队随机抓人、表决抛硬币、任务牌守阵营合法性、刺杀在
    "非同伙"里随机指认），extract 阶段吐能过 parse_json 的 JSON。

    同一个动作的 converse 与 extract 两次调用，因 view 相同而派生**同一个**决策（种子取自
    席位号+任务+组队轮次+阶段），故自由文本里写的队伍/票与抽取出的结构化结果永远自洽——
    没有"嘴上说带 1、3、5，结构化却是 2、4、6"的错位。"""

    def __init__(self, rng: random.Random) -> None:
        self._salt = rng.getrandbits(64)  # 整局固定，叠进每个动作的种子，让不同 --seed 玩得不同

    async def call(self, phase: str, view: PlayerView, spec: ModelSpec, messages: list[BaseMessage]) -> BaseMessage:
        base = phase.removesuffix(".extract")
        decision = self._decide(base, view)
        content = decision.model_dump_json() if phase.endswith(".extract") else _render(base, decision)
        return AIMessage(content=content)

    def _decide(self, base: str, view: PlayerView) -> BaseModel:
        """对一个动作派生确定且合法的结构化决策。种子只取动作的稳定标识，故两次调用结果一致。"""
        r = random.Random(f"{self._salt}:{view.seat_no}:{view.quest_no}:{view.attempt}:{base}")
        if base == "propose":
            team = sorted(r.sample(range(1, view.players + 1), view.team_size))
            nos = "、".join(f"{no}号" for no in team)
            return Proposal(team=team, speech=f"（随机）我看 {nos} 就挺合适，出发。", monologue="（随机）闭眼点的将。")
        if base == "discuss":
            return Statement(speech="（随机）这队我说不上好坏，先看看大家的票。", monologue="（随机）水一句，别看我。")
        if base == "team_vote":
            return TeamVote(approve=r.random() < 0.5, monologue="（随机）抛硬币定的。")
        if base == "quest":
            success = True if view.role.side is Side.GOOD else r.random() < 0.5
            return QuestCard(success=success, monologue="（随机）牌已扣下，面无表情。")
        if base == "assassinate":
            target = r.choice(view.good_suspects())
            return Assassination(target_no=target, speech=f"（随机）直觉告诉我，{target}号就是梅林。", monologue="（随机）蒙的，但要蒙得自信。")
        if base == "lady_choose":
            return LadyChoice(target_no=r.choice(view.lady_candidates()), monologue="（随机）就查你了。")
        if base == "lady_announce":
            claim = view.lady_seen if r.random() < 0.5 else ("evil" if view.lady_seen == "good" else "good")
            side = "蓝方" if claim == "good" else "红方"
            return Statement(speech=f"（随机）我查验了{view.lady_target}号——{side}。", monologue="（随机）信不信由你们。")
        if base == "banter":
            return Statement(speech="（随机）这局打完了，各位辛苦，下把再战。", monologue="（随机）复盘就免了，赢牌全靠脸。")
        raise ValueError(f"未知决策阶段：{base}")  # 纵深防御：调用方与 _render 应已穷举八个阶段


def _render(base: str, d: BaseModel) -> str:
    """把假后端的结构化决策渲染回"标签行收尾"的自由文本——供线程/回放占位，体例与真玩家一致。"""
    if isinstance(d, Proposal):
        return f"内心戏：{d.monologue}\n发言：{d.speech}\n队伍：{'、'.join(map(str, d.team))}"
    if isinstance(d, TeamVote):
        return f"内心戏：{d.monologue}\n投票：{'赞成' if d.approve else '否决'}"
    if isinstance(d, QuestCard):
        return f"内心戏：{d.monologue}\n出牌：{'成功' if d.success else '破坏'}"
    if isinstance(d, Assassination):
        return f"内心戏：{d.monologue}\n发言：{d.speech}\n刺杀：{d.target_no}"
    if isinstance(d, LadyChoice):
        return f"内心戏：{d.monologue}\n查验：{d.target_no}"
    assert isinstance(d, Statement)  # discuss / lady_announce 共用：只有发言与内心戏
    return f"内心戏：{d.monologue}\n发言：{d.speech}"


# 重试参数：只兜 LLM 这层外部不可控依赖。_MAX_ATTEMPTS 含首次（1 次正常 + 至多 4 次重试）。
_MAX_ATTEMPTS = 5
_BACKOFF_BASE = 1.0  # 秒：指数退避基数（第 n 次重试前等 base*2^(n-1)，封顶 _BACKOFF_CAP，叠抖动）
_BACKOFF_CAP = 30.0  # 秒：单次等待上限


class LLMPlayer:
    """玩家模型自由文本对话 + 固定轻量 extractor 解析校验（见 parsing.py）。模型调用本身下沉到
    ModelBackend——真模型走 LiveBackend，离线调试走 FakeBackend，本类对此**一无所知**。

    玩家模型不写 JSON（自由发挥不耗脑力在格式上）；extractor 是我们自己选定的单一模型，
    JSON 由它产出、parse_json 解析校验——跨厂商接玩家模型不会再踩 response_format 的坑。

    容错铁律分两层，区别只在"这东西可不可控"：
    - **我们自己完全可控的代码**（核心逻辑、状态机、解析后的结构化数据）：严禁默认兜底——非法
      态在边界就该被拦死，下游拿到的默认已合法，违例即大声崩溃，绝不静默替换成占位/随机内容。
    - **LLM 调用这种外部不可控依赖**：必须鲁棒。模型偶发抽风（超时、网络抖动、不守格式、该有
      发言/内心戏却空了）不是 bug 而是常态，对它要退避重试（见 _act），而非一次失败就让整局炸。
      重试到顶仍不行，才回到上一条——大声抛错，不静默兜底。
    """

    def __init__(self, recorder: Recorder, extractor: ModelSpec, backend: ModelBackend) -> None:
        self._recorder = recorder
        self._extractor_spec = extractor
        self._backend = backend

    async def _invoke(self, phase: str, view: PlayerView, spec: ModelSpec, messages: list[BaseMessage]) -> str:
        """单次调用 + 落盘（成功/失败都记，若开了 --replay）。返回原文文本。"""
        t0 = perf_counter()
        try:
            resp = await self._backend.call(phase, view, spec, messages)
        except Exception as e:
            self._record(phase, view, spec, messages, None, None, round((perf_counter() - t0) * 1000), f"API 调用失败：{e}")
            raise
        latency_ms = round((perf_counter() - t0) * 1000)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        self._record(phase, view, spec, messages, text, resp, latency_ms, None)
        return text

    async def _converse(self, phase: str, view: PlayerView) -> str:
        """玩家模型：整局固定 system + 该席位线程 + 本轮提问，自由文本作答。"""
        spec = view.model  # llm 后端下必非 None：_check_env 已在边界保证
        history = [HumanMessage(m.content) if m.role == "human" else AIMessage(m.content) for m in view.messages]
        messages: list[BaseMessage] = [SystemMessage(prompts.system_text(view)), *history, HumanMessage(view.prompt)]
        return await self._invoke(phase, view, spec, messages)

    async def _extract[T: BaseModel](self, phase: str, view: PlayerView, instruction: str, model_cls: type[T]) -> T:
        """extractor：把玩家自由文本解析成结构化对象。解析失败 / 字段不合法（含 NonEmptyStr 触发
        的"发言或内心戏为空"）即抛 JsonParseError——交由外层 _act 退避重试，本层不兜底。"""
        messages: list[BaseMessage] = [SystemMessage(prompts.EXTRACT_SYSTEM), HumanMessage(instruction)]
        text = await self._invoke(f"{phase}.extract", view, self._extractor_spec, messages)
        try:
            return parse_json(text, model_cls)
        except JsonParseError as e:
            self._record(f"{phase}.extract", view, self._extractor_spec, messages, text, None, 0, str(e))
            raise

    async def _act[R](self, phase: str, view: PlayerView, once: Callable[[], Awaitable[R]]) -> R:
        """对外部不可控依赖（LLM）的鲁棒性边界：跑一次完整决策（玩家发挥 → 抽取 → 校验），任何
        环节"不符预期"都退避重试，最多 _MAX_ATTEMPTS 次。

        哪些算"不符预期"——全在 once() 抛出的异常里，不需要本层逐一辨认：
          · API 调用失败 / 超时（LiveBackend 抛）；
          · extractor 抽不出合法 JSON、或字段不满足约束——含"该有发言/内心戏却空了"
            （NonEmptyStr → ValidationError → JsonParseError）。
        每次失败尝试都已在 _invoke/_extract 里落盘（ok=false），故重试不丢审计。连试到顶仍败，
        才把最后一个异常抛出——回到"完全可控处不静默兜底"的铁律，让整局大声崩而非带病硬撑。"""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return await once()
            except Exception as e:
                if attempt == _MAX_ATTEMPTS:
                    raise
                delay = min(_BACKOFF_BASE * 2 ** (attempt - 1), _BACKOFF_CAP) + random.random()
                print(
                    f"[retry] 座位{view.seat_no} {phase} 第 {attempt}/{_MAX_ATTEMPTS} 次失败，"
                    f"{delay:.1f}s 后重试：{e}",
                    file=sys.stderr,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable: 循环要么 return 要么在末次 raise")  # 纵深防御

    def _record(
        self,
        phase: str,
        view: PlayerView,
        spec: ModelSpec,
        messages: list[BaseMessage],
        text: str | None,
        resp: BaseMessage | None,
        latency_ms: int,
        error: str | None,
    ) -> None:
        usage = dict(resp.usage_metadata) if (resp is not None and getattr(resp, "usage_metadata", None)) else {}
        self._recorder.record_llm_call(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "call_id": decision_id(view.quest_no, view.attempt, phase, view.seat_no),
                "quest": view.quest_no,
                "attempt": view.attempt,
                "phase": phase,  # 含 `.extract` 后缀者为抽取调用；同决策的主/抽取共享 call_id
                "seat_no": view.seat_no,
                "model": spec.model_id,
                "messages": [{"role": _ROLE.get(type(m), "?"), "content": m.content} for m in messages],
                "output": text,
                "usage": usage,
                "latency_ms": latency_ms,
                "ok": error is None,
                "error": error,
            }
        )

    # 八个决策动作：每个把"玩家发挥 → 抽取"包成一次 once()，交 _act 退避重试。retry 重跑的是
    # 整次决策（含玩家发挥）——因为"发言为空"这类病在玩家原文里，只重抽取救不回来，得让玩家重答。

    async def propose(self, view: PlayerView) -> tuple[Proposal, str]:
        async def once() -> tuple[Proposal, str]:
            raw = await self._converse("propose", view)
            return await self._extract("propose", view, prompts.extract_propose(raw, view.team_size, view.players), Proposal), raw

        return await self._act("propose", view, once)

    async def discuss(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._converse("discuss", view)
            return await self._extract("discuss", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("discuss", view, once)

    async def vote_team(self, view: PlayerView) -> tuple[TeamVote, str]:
        async def once() -> tuple[TeamVote, str]:
            raw = await self._converse("team_vote", view)
            return await self._extract("team_vote", view, prompts.extract_vote(raw), TeamVote), raw

        return await self._act("team_vote", view, once)

    async def play_quest(self, view: PlayerView) -> tuple[QuestCard, str]:
        async def once() -> tuple[QuestCard, str]:
            raw = await self._converse("quest", view)
            return await self._extract("quest", view, prompts.extract_quest(raw), QuestCard), raw

        return await self._act("quest", view, once)

    async def assassinate(self, view: PlayerView) -> tuple[Assassination, str]:
        async def once() -> tuple[Assassination, str]:
            raw = await self._converse("assassinate", view)
            return await self._extract("assassinate", view, prompts.extract_assassinate(raw, view.good_suspects()), Assassination), raw

        return await self._act("assassinate", view, once)

    async def lady_choose(self, view: PlayerView) -> tuple[LadyChoice, str]:
        async def once() -> tuple[LadyChoice, str]:
            raw = await self._converse("lady_choose", view)
            return await self._extract("lady_choose", view, prompts.extract_lady_choice(raw, view.lady_candidates()), LadyChoice), raw

        return await self._act("lady_choose", view, once)

    async def lady_announce(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._converse("lady_announce", view)
            return await self._extract("lady_announce", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("lady_announce", view, once)

    async def banter(self, view: PlayerView) -> tuple[Statement, str]:
        async def once() -> tuple[Statement, str]:
            raw = await self._converse("banter", view)
            return await self._extract("banter", view, prompts.extract_statement(raw), Statement), raw

        return await self._act("banter", view, once)


def build_player(kind: Literal["llm", "random"], rng: random.Random, recorder: Recorder, extractor: ModelSpec) -> Player:
    """工厂：唯一的 Player 是 LLMPlayer，按 kind 在边界注入模型后端——'llm' → LiveBackend
    （真发请求）；'random' → FakeBackend（离线假后端，零 key 跑通整条链路）。"""
    backend: ModelBackend = FakeBackend(rng) if kind == "random" else LiveBackend()
    return LLMPlayer(recorder=recorder, extractor=extractor, backend=backend)
