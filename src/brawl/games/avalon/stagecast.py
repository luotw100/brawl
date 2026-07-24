"""阿瓦隆演出投影：把存档重放成舞台指令流，并交付展示态 reducer 与录制挂钩。

这是"阿瓦隆怎么上演"的唯一落脚点。投影纪律：

- 一切规则事实（票型/任务牌/刺杀命中/胜负/阵营）只取自 action payload——它们是
  rules 在生成时冻结的 god-view 真相。这里绝不重新计票、绝不反解 turn_id。
- 少量展示上下文（quest/attempt 计数、当轮票/牌的独白、仙女查验缓存）随流累积，
  是纯显示组装：独白从未进过 rules 账本，只能在这里随流收集。
- 未知 kind 直接抛：投影漂移要立刻暴露，不静默吞（旧桥放过 evil_chat 的教训）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from brawl.games.avalon.protocol import (
    Assassination,
    AvalonScene,
    Deal,
    GameOpen,
    GameOver,
    LadyCheck,
    LadyChoose,
    ProposalLog,
    QuestCard,
    QuestLog,
    QuestReveal,
    SeatInfo,
    SeatVote,
    Speech,
    VoteReveal,
)
from brawl.games.avalon.validation import AvalonReplayMeta
from brawl.runtime.types import Sentence
from brawl.stage.protocol import BaseScene, Directive, FeedItem, HostSpeech
from brawl.stage.stagecast import MediaResolver, StageGame

_PHASE = {"propose": "提案", "discuss": "讨论", "lady": "仙女发言", "banter": "闲聊", "evil_chat": "密谋"}


def stagecast(sentences: list[Sentence], meta: dict[str, Any], media: MediaResolver) -> list[Directive]:
    """存档 → 舞台指令流（纯投影）。"""
    projector = _Projector(AvalonReplayMeta.model_validate_json(json.dumps(meta), strict=True), media)
    out: list[Directive] = []
    for sent in sentences:
        out.extend(projector.feed(sent))
    return out


class _Projector:
    """逐句投影器：buf_* 是当轮展示缓存，ctx_* 是显示序号（quest/attempt）。"""

    def __init__(self, meta: AvalonReplayMeta, media: MediaResolver) -> None:
        self._meta = meta
        self._media = media
        self._seats: dict[int, SeatInfo] = {}
        self._quest = 1
        self._attempt = 1
        self._team: list[int] = []
        self._buf_votes: dict[int, tuple[bool, str]] = {}  # seat → (approve, monologue)
        self._buf_cards: dict[int, tuple[bool, str]] = {}  # seat → (success, monologue)
        self._buf_lady: tuple[int, str] | None = None  # (target_no, choose_monologue)

    # ---------------------------------------------------------------- 主分派

    def feed(self, sent: Sentence) -> list[Directive]:
        action = json.loads(sent.action) if sent.action else {}
        kind = action.get("kind")
        if sent.speaker == "host":
            return self._host(sent, action, kind)
        return self._player(sent, action, kind)

    def _player(self, sent: Sentence, a: dict[str, Any], kind: str | None) -> list[Directive]:
        seat = sent.speaker
        assert isinstance(seat, int)
        call_id = _call_id(sent)
        if kind == "propose":
            self._team = list(a["team"])
            return [self._speech(sent, "propose", a, quest=self._quest, attempt=self._attempt, team=self._team)]
        if kind == "discuss":
            return [self._speech(sent, "discuss", a, quest=self._quest, attempt=self._attempt)]
        if kind == "team_vote":
            self._buf_votes[seat] = (a["approve"], a["monologue"])
            return []
        if kind == "quest":
            self._buf_cards[seat] = (a["success"], a["monologue"])
            return []
        if kind == "lady_choose":
            self._buf_lady = (a["target_no"], a["monologue"])
            return [LadyChoose(holder_no=seat, target_no=a["target_no"], choose_monologue=a["monologue"])]
        if kind == "lady_announce":
            assert self._buf_lady is not None, "lady_announce 之前缺 lady_choose"
            target, _ = self._buf_lady
            self._buf_lady = None
            return [
                LadyCheck(holder_no=seat, target_no=target, truth=a["truth"]),
                self._speech(sent, "lady", a, lady_target=target, lady_truth=a["truth"]),
            ]
        if kind == "evil_chat":
            return [self._speech(sent, "evil_chat", a)]
        if kind == "assassinate":
            return [
                Assassination(
                    assassin_no=seat, target_no=a["target_no"], hit=a["hit"],
                    text=a["speech"], monologue=a["monologue"],
                    call_id=call_id, audio_url=self._audio(call_id),
                )
            ]
        if kind == "banter":
            return [self._speech(sent, "banter", a)]
        raise ValueError(f"avalon 投影遇到未知玩家 kind：{kind}（rules 契约变了，stagecast 要跟上）")

    def _host(self, sent: Sentence, a: dict[str, Any], kind: str | None) -> list[Directive]:
        if kind == "game_start":
            self._seats = {s["no"]: self._seat(s) for s in a["seats"]}
            seats = [self._seats[no] for no in sorted(self._seats)]
            return [
                GameOpen(players=a["players"], good=a["good"], evil=a["evil"], seats=seats),
                self._host_speech(sent, "opening"),
                Deal(seats=seats, leader_no=self._meta.setup.first_leader, lady_holder=self._meta.setup.lady_holder),
            ]
        if kind == "team_intro":
            # beat 名对齐 plan.yaml 的 lead_in_next 槽位（旧桥叫 "team"，丢了前置贴靠）。
            return [self._host_speech(sent, "propose_intro")]
        if kind == "vote_result":
            votes = [
                SeatVote(seat_no=seat, approve=approve, monologue=monologue)
                for seat, (approve, monologue) in sorted(self._buf_votes.items())
            ]
            self._buf_votes = {}
            reveal = VoteReveal(
                quest=self._quest, attempt=self._attempt, votes=votes,
                approved=a["approved"], rejections=a["rejections"],
            )
            if not a["approved"]:
                self._attempt += 1
            return [reveal, self._host_speech(sent, "vote_result")]
        if kind == "quest_result":
            cards = [
                QuestCard(seat_no=seat, success=success, monologue=monologue)
                for seat, (success, monologue) in sorted(self._buf_cards.items())
            ]
            self._buf_cards = {}
            reveal = QuestReveal(
                quest=a["quest"], team=list(self._team), cards=cards,
                fails=a["fails"], fails_required=a["fails_required"], result=a["result"],
            )
            self._quest = a["quest"] + 1
            self._attempt = 1
            self._team = []
            return [reveal, self._host_speech(sent, "quest_result")]
        if kind == "lady_transfer":
            return [self._host_speech(sent, "lady")]
        if kind == "lady_choice":
            return [self._host_speech(sent, "lady_choice")]
        if kind == "evil_chat_opening":
            return [self._host_speech(sent, "evil_chat_opening")]
        if kind == "assassination_result":
            return [self._host_speech(sent, "assassination")]
        if kind == "reveal":
            # 上帝视角全程亮身份，主持人复述身份不上演（其 TTS 也随之不播）。
            return [
                GameOver(
                    winner=a["winner"], win_reason=a["win_reason"],
                    seats=[self._seats[no] for no in sorted(self._seats)],
                )
            ]
        if kind == "closing":
            return [self._host_speech(sent, "closing")]
        raise ValueError(f"avalon 投影遇到未知 host kind：{kind}（rules 契约变了，stagecast 要跟上）")

    # ---------------------------------------------------------------- 组装件

    def _seat(self, s: dict[str, Any]) -> SeatInfo:
        setup = self._meta.setup
        no = s["no"]
        model = setup.models[no - 1]
        return SeatInfo(
            no=no, name=setup.names[no - 1], persona=s["persona"],
            role=s["role"], side=s["side"], model=model,
            avatar_url=self._media.avatar(no),
            model_display=self._meta.model_labels.get(model),
        )

    def _speech(self, sent: Sentence, phase: str, a: dict[str, Any], **extra: Any) -> Speech:
        call_id = _call_id(sent)
        seat = sent.speaker
        assert isinstance(seat, int)
        return Speech(
            phase=phase, seat_no=seat, text=a["speech"], monologue=a["monologue"],
            call_id=call_id, audio_url=self._audio(call_id), **extra,
        )

    def _host_speech(self, sent: Sentence, beat: str) -> HostSpeech:
        call_id = _call_id(sent)
        return HostSpeech(
            name=self._meta.setup.host_name, beat=beat, text=_spoken_text(sent),
            call_id=call_id, audio_url=self._audio(call_id),
        )

    def _audio(self, call_id: str) -> str | None:
        resolved = self._media.audio(call_id)
        return resolved[0] if resolved else None


# ---------------------------------------------------------------- reducer / 录制挂钩（展示态折叠）


def new_scene() -> AvalonScene:
    return AvalonScene()


def apply(scene: BaseScene, item: FeedItem) -> None:
    """把一拍折进阿瓦隆状态（feed 入队与窗口护栏由引擎统一做，这里只折玩法态）。"""
    assert isinstance(scene, AvalonScene)
    directive = item.directive
    scene.speaking = None

    match directive:
        case GameOpen():
            # 新局开场：头像就座但身份面朝下（dealt=False）。
            scene.seats = directive.seats
            scene.dealt = False
            scene.leader_no = None
            scene.lady_holder = None
            scene.quest_no = 1
            scene.attempt = 1
            scene.missions = [None] * 5
            scene.rejections = 0
            scene.team = []
            scene.winner = None
            scene.win_reason = None
            scene.timeline = []
        case Deal():
            scene.seats = directive.seats
            scene.dealt = True  # 发牌翻面：身份亮出（此后整局常亮）
            scene.leader_no = directive.leader_no
            scene.lady_holder = directive.lady_holder
        case Speech():
            scene.speaking = directive.seat_no
            if directive.quest is not None:
                scene.quest_no = directive.quest
                scene.attempt = directive.attempt or scene.attempt
            if directive.team is not None:
                scene.team = directive.team
                scene.leader_no = directive.seat_no
            if directive.lady_target is not None:
                scene.lady_holder = directive.lady_target  # 令牌传给被查者
        case LadyChoose():
            scene.speaking = directive.holder_no
        case LadyCheck():
            scene.speaking = directive.holder_no
            scene.lady_holder = directive.target_no  # 本拍内由前端按 directive 演令牌交接
        case VoteReveal():
            # 里程碑入账要在清队之前（被否会清空 scene.team）。leader/team 由刚才的 propose 拍落定。
            scene.timeline.append(ProposalLog(
                quest=directive.quest, attempt=directive.attempt,
                leader_no=scene.leader_no, team=list(scene.team), approved=directive.approved,
                approves=[v.seat_no for v in directive.votes if v.approve],
                rejects=[v.seat_no for v in directive.votes if not v.approve],
            ))
            scene.rejections = directive.rejections
            if not directive.approved:
                scene.team = []
        case QuestReveal():
            scene.timeline.append(QuestLog(
                quest=directive.quest, team=list(directive.team), result=directive.result,
                fails=directive.fails, fail_seats=[c.seat_no for c in directive.cards if not c.success],
            ))
            scene.missions[directive.quest - 1] = directive.result
            scene.rejections = 0
            scene.team = []
        case Assassination():
            scene.speaking = directive.assassin_no
        case GameOver():
            scene.winner = directive.winner
            scene.win_reason = directive.win_reason
        case HostSpeech():
            pass  # 主持人不占座位：speaking 已清空，台词只进 feed（作为旁白挂在相邻拍上）


def is_opening_anchor(directive: Directive) -> bool:
    return isinstance(directive, Deal)  # 录制等"发牌"画面 paint 出来再开抓，对齐视频零点


def terminal(directive: Directive) -> bool:
    return isinstance(directive, GameOver)


def summary(directive: Directive, scene: BaseScene) -> tuple[str, str, str]:
    """一拍 → (标签, 颜色, 详情)，给录制进度条打成一行。"""
    seats = getattr(scene, "seats", [])
    match directive:
        case GameOpen():
            return "开局", "cyan", f"{directive.players} 人 · 善 {directive.good} / 恶 {directive.evil}"
        case Deal():
            return "发牌", "cyan", f"队长 {directive.leader_no} 号"
        case HostSpeech():
            return "主持", "yellow", f"{directive.name}：{_snip(directive.text)}"
        case Speech():
            return _PHASE.get(directive.phase, directive.phase), "green", f"{_seat_name(seats, directive.seat_no)}：{_snip(directive.text)}"
        case LadyChoose():
            return "选验", "green", f"{_seat_name(seats, directive.holder_no)} 选择 {_seat_name(seats, directive.target_no)}"
        case LadyCheck():
            return "验人", "green", f"{_seat_name(seats, directive.holder_no)} 查 {_seat_name(seats, directive.target_no)}"
        case VoteReveal():
            return "表决", "magenta", f"{'通过' if directive.approved else '否决'} · 第 {directive.quest}.{directive.attempt} 队"
        case QuestReveal():
            return "任务", "blue", f"第 {directive.quest} 任务 {directive.result}（{directive.fails} 失败）"
        case Assassination():
            return "刺杀", "red", "命中" if directive.hit else "落空"
        case GameOver():
            return "终局", "bold red", f"{directive.winner} 胜 · {directive.win_reason}"
        case _:
            return "?", "white", type(directive).__name__


STAGE_GAME = StageGame(
    id="avalon",
    title="阿瓦隆",
    stagecast=stagecast,
    new_scene=new_scene,
    apply=apply,
    summary=summary,
    is_opening_anchor=is_opening_anchor,
    terminal=terminal,
    plan_path=Path(__file__).parent / "plan.yaml",
)


# ---------------------------------------------------------------- 小工具


def _call_id(sent: Sentence) -> str:
    return f"{sent.seq}.{sent.speaker}"


def _spoken_text(sent: Sentence) -> str:
    """host 句子的口播文本：只取 spoken=True 的 delivery（票型账本类 spoken=False 不进旁白）。"""
    return "\n".join(d.text for d in sent.deliveries if d.spoken and d.text)


def _snip(text: str, n: int = 36) -> str:
    """台词压成一行预览：折叠空白、超长截断（节拍是进度而非全文）。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _seat_name(seats: list[SeatInfo], seat_no: int) -> str:
    return next((s.name for s in seats if s.no == seat_no), f"{seat_no}号")
