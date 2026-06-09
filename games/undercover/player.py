"""Player：席位背后的"脑子"，可替换的决策后端（LangGraph 注入的 Deps）。

- LLMPlayer：真模型在玩，按该席位运行时配的模型 + 人设决策（langchain-openai）。
- RandomPlayer：随机决策，无需联网——既是占位也是 mock，用于 --player random 跑通/测试。

Player 只看 PlayerView 里的"该席位当前可见信息"，绝不接触别人的身份/词/独白。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from undercover import prompts
from undercover.parsing import JsonParseError, parse_json
from undercover.recorder import Recorder
from undercover.registry import ModelSpec, Registry
from undercover.state import Describe, Utterance, Vote

_ROLE = {SystemMessage: "system", HumanMessage: "human", AIMessage: "assistant"}


@dataclass
class PlayerView:
    """喂给 Player 的、某席位当前能看到的全部信息。"""

    seat_no: int
    name: str
    word: str
    persona_system: str
    strategy_system: str
    model: ModelSpec | None  # llm 后端用；random 后端忽略
    roster: list[tuple[int, str]]  # 在场席位 (no, name)，含自己
    transcript: list[Utterance]  # 截至当前的公开发言
    round: int

    def others(self) -> list[int]:
        return [no for no, _ in self.roster if no != self.seat_no]


class Player(Protocol):
    async def describe(self, view: PlayerView) -> Describe: ...
    async def vote(self, view: PlayerView) -> Vote: ...


class RandomPlayer:
    """随机后端：不联网、形状合法。描述用占位句，投票随机投一个在场他人。"""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng

    async def describe(self, view: PlayerView) -> Describe:
        return Describe(
            description=f"（随机）这东西嘛……反正跟「{view.word[:1]}」沾点边，懂的都懂。",
            monologue=f"（随机）我也不知道自己是不是异类，{view.name}先稳住。",
        )

    async def vote(self, view: PlayerView) -> Vote:
        target = self._rng.choice(view.others())
        return Vote(target_no=target, reason=f"（随机）总得投一个，{target}号看着可疑。", monologue="（随机）闭眼投。")


class LLMPlayer:
    """真模型后端：langchain-openai 普通补全 + 自解析校验（见 parsing.py）。

    刻意不用 with_structured_output——纯靠 prompt 约束 JSON、自己解析，跨厂商最通用，
    接新模型不会再踩 response_format/tool_choice 的坑。每席位按其 ModelSpec 现建模型。

    无兜底：API 调用失败、超时、JSON 解析失败一律抛错（连同完整调用记录落盘），
    让问题立刻暴露，绝不静默替换成占位/随机内容。
    """

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def _model(self, spec: ModelSpec) -> ChatOpenAI:
        import os

        return ChatOpenAI(
            model=spec.model_id,
            base_url=spec.base_url,
            api_key=os.environ[spec.api_key_env],  # 必存在：validate_settings 已在边界校验
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
            timeout=spec.timeout,
            extra_body=spec.extra_body or None,
        )

    async def _ask[T: BaseModel](self, phase: str, view: PlayerView, system: str, user: str, model_cls: type[T]) -> T:
        """普通文本补全 → 自己解析校验成结构化对象。失败即抛错，不重试、不兜底。

        每次调用都落一条记录到 llm_calls.jsonl（成功/失败都记，若开了 --replay）。
        """
        spec = view.model  # llm 后端下必非 None：validate_settings 已在边界保证
        model = self._model(spec)
        messages: list[BaseMessage] = [SystemMessage(content=system), HumanMessage(content=user)]

        t0 = perf_counter()
        try:
            resp = await model.ainvoke(messages)
        except Exception as e:
            self._record(phase, view, spec, messages, None, None, round((perf_counter() - t0) * 1000), None, f"API 调用失败：{e}")
            raise
        latency_ms = round((perf_counter() - t0) * 1000)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        try:
            parsed = parse_json(text, model_cls)
        except JsonParseError as e:
            self._record(phase, view, spec, messages, text, resp, latency_ms, None, str(e))
            raise
        self._record(phase, view, spec, messages, text, resp, latency_ms, parsed, None)
        return parsed

    def _record(
        self,
        phase: str,
        view: PlayerView,
        spec: ModelSpec,
        messages: list[BaseMessage],
        text: str | None,
        resp: BaseMessage | None,
        latency_ms: int,
        parsed: BaseModel | None,
        error: str | None,
    ) -> None:
        usage = dict(resp.usage_metadata) if (resp is not None and getattr(resp, "usage_metadata", None)) else {}
        self._recorder.record_llm_call(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "round": view.round,
                "phase": phase,
                "seat_no": view.seat_no,
                "name": view.name,
                "model": spec.model_id,
                "messages": [{"role": _ROLE.get(type(m), "?"), "content": m.content} for m in messages],
                "output": text,
                "usage": usage,
                "latency_ms": latency_ms,
                "ok": error is None,
                "parsed": parsed.model_dump() if parsed is not None else None,
                "error": error,
            }
        )

    async def describe(self, view: PlayerView) -> Describe:
        return await self._ask("describe", view, prompts.system_prompt(view.persona_system, view.strategy_system), prompts.describe_prompt(view), Describe)

    async def vote(self, view: PlayerView) -> Vote:
        return await self._ask("vote", view, prompts.system_prompt(view.persona_system, view.strategy_system), prompts.vote_prompt(view), Vote)


def build_player(kind: str, registry: Registry, rng: random.Random, recorder: Recorder) -> Player:
    """工厂：'random' → RandomPlayer；'llm' → LLMPlayer（带可选 LLM 调用落盘）。"""
    if kind == "random":
        return RandomPlayer(rng)
    if kind == "llm":
        return LLMPlayer(recorder=recorder)
    raise ValueError(f"未知 player 后端: {kind}")
