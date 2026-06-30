"""阿瓦隆插件：把 replay 的 god-view 事件翻成舞台指令、折进 AvalonScene，并交付节奏/音效/进度。

这是引擎里唯一懂"阿瓦隆"的地方——engine/record/registry 全是玩法无关的，只通过 GamePlugin
认识它。将来加玩法 = 并列一个同样实现 GamePlugin 的插件，引擎零分叉。

v2"电视信号"语义下 `apply` 是该玩法的 reducer：引擎每拍 adapt → 折进 Scene → 广播完整 Scene；
客户端是纯渲染函数，不持有状态、不做折叠。feed 入队与窗口护栏由引擎统一做（玩法无关），
这里只折阿瓦隆状态。
"""

from __future__ import annotations

from typing import Any

from server import replay
from server.games.avalon.protocol import (
    Assassination,
    AvalonScene,
    Deal,
    GameOpen,
    GameOver,
    ProposalLog,
    QuestCard,
    QuestLog,
    QuestReveal,
    SeatInfo,
    SeatVote,
    Speech,
    VoteReveal,
)
from server.games.base import AdaptContext
from server.protocol import (
    BaseScene,
    Directive,
    FeedItem,
    GameResult,
    HostSpeech,
    Intermission,
)

# pace=1 时的每字停顿上限（与 replay.py 的口径一致）
_MAX_CHAR_DELAY = 0.06

_PHASE = {"propose": "提案", "discuss": "讨论", "lady": "验人", "banter": "闲聊"}


class AvalonPlugin:
    """GamePlugin 实现：阿瓦隆。无状态，所有随局数据经 AdaptContext / 入参传入。"""

    id = "avalon"

    # ---------------------------------------------------------------- 适配 / 折叠

    def new_scene(self) -> AvalonScene:
        return AvalonScene()

    def adapt(self, event: dict[str, Any], ctx: AdaptContext) -> Directive | None:
        """一条 replay 事件 → 一条舞台指令。未知事件类型直接抛——契约变了要立刻暴露，不静默吞。"""
        t = event["type"]
        if t == "host":
            if event["beat"] == "reveal":
                return None  # 上帝视角全程亮身份，主持人复述身份纯属多余——这一拍不演（其 TTS 也随之不播）
            return HostSpeech(
                name=ctx.host_name, beat=event["beat"], text=event["text"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "game_start":
            return GameOpen(
                players=event["players"], good=event["good"], evil=event["evil"],
                seats=[self._seat(s, ctx) for s in event.get("seats", [])],  # 老 replay 无 seats → 空
            )
        if t == "deal":
            return Deal(
                seats=[self._seat(s, ctx) for s in event["seats"]],
                leader_no=event["leader_no"],
                lady_holder=event["lady_holder"],
            )
        if t == "propose":
            return Speech(
                phase="propose", seat_no=event["leader_no"], text=event["speech"], monologue=event["monologue"],
                quest=event["quest"], attempt=event["attempt"], team=event["team"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "discuss":
            return Speech(
                phase="discuss", seat_no=event["seat_no"], text=event["text"], monologue=event["monologue"],
                quest=event["quest"], attempt=event["attempt"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "vote_result":
            return VoteReveal(
                quest=event["quest"], attempt=event["attempt"],
                votes=[SeatVote(seat_no=v["seat_no"], approve=v["approve"], monologue=v["monologue"]) for v in event["votes"]],
                approved=event["approved"], rejections=event["rejections"],
            )
        if t == "quest_result":
            return QuestReveal(
                quest=event["quest"], team=event["team"],
                cards=[QuestCard(seat_no=c["seat_no"], success=c["success"], monologue=c["monologue"]) for c in event["cards"]],
                fails=event["fails"], fails_required=event["fails_required"], result=event["result"],
            )
        if t == "lady":
            return Speech(
                phase="lady", seat_no=event["holder_no"], text=event["speech"], monologue=event["monologue"],
                lady_target=event["target_no"], lady_truth=event["truth"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("announce_call_id"), ctx.tts),
            )
        if t == "assassination":
            return Assassination(
                assassin_no=event["assassin_no"], target_no=event["target_no"], hit=event["hit"],
                text=event["speech"], monologue=event["monologue"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        if t == "game_end":
            return GameOver(
                winner=event["winner"], win_reason=event["win_reason"],
                seats=[self._seat(s, ctx) for s in event["seats"]],
            )
        if t == "banter":
            return Speech(
                phase="banter", seat_no=event["seat_no"], text=event["text"], monologue=event["monologue"],
                audio_url=replay.audio_url(ctx.replay_id, event.get("call_id"), ctx.tts),
            )
        raise ValueError(f"未知事件类型：{t}（events 契约变了，adapter 要跟上）")

    def _seat(self, s: dict[str, Any], ctx: AdaptContext) -> SeatInfo:
        return SeatInfo(
            no=s["no"], name=ctx.names.get(s["persona"], s["persona"]), persona=s["persona"],
            role=s["role"], side=s["side"], model=s.get("model", ""),
            avatar_url=ctx.avatars.get(s["no"]),
            model_display=ctx.model_labels.get(s.get("model") or ""),
        )

    def apply(self, scene: AvalonScene, item: FeedItem) -> None:
        """把一拍折进阿瓦隆状态（feed 入队与窗口护栏由引擎统一做，这里只折玩法态）。"""
        directive = item.directive
        scene.speaking = None

        match directive:
            case GameOpen():
                # 新局开场：清掉上一局残留。头像就座但身份面朝下（dealt=False），seats 可空（老 replay）。
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
            case Intermission():
                pass  # speaking 已清空

    # ---------------------------------------------------------------- 节奏

    def content_seconds(self, directive: Directive, pace: float) -> float:
        """主内容秒数（视觉动画/朗读，已含 pace 缩放，未计音频地板与 speed）。基准节奏写死在此：
        发言按字数、发牌每张约 0.75s、表决逐个亮票、大场面敢留白。pace=0 全归零（整局瞬间倾倒）。"""
        char = pace * _MAX_CHAR_DELAY
        unit = pace
        match directive:
            case Speech(text=text, monologue=monologue):
                return (len(text) + len(monologue)) * char
            case Assassination(text=text, monologue=monologue):
                return (len(text) + len(monologue)) * char
            case GameOpen():
                return unit * 4.0
            case Deal():
                return unit * (len(directive.seats) * 0.75 + 0.5)  # 逐张发牌 ~0.75s/张（2x 加速，前端 stagger/fly 由 dur 反推自动跟随）
            case VoteReveal():
                return unit * (len(directive.votes) * 0.8 + 2.5)  # 逐个亮票 + 判定 hold
            case QuestReveal():
                return unit * 6.0  # 慢起→摊牌→翻轨→闪屏
            case GameOver():
                return unit * 12.0  # 逐个翻底牌的大结局
            case _:
                raise ValueError(f"未覆盖的指令类型：{type(directive).__name__}")

    def tail_seconds(self, directive: Directive, pace: float) -> float:
        """尾部气口秒数（已含 pace 缩放，未计 speed）：给有台词的拍留发言间留白。"""
        tail = {Speech: 3.0, Assassination: 6.0}.get(type(directive), 0.0)
        return pace * tail

    def reveal_paced_by_narration(self, directive: Directive) -> bool:
        return isinstance(directive, (Deal, VoteReveal, QuestReveal))

    def speech_paced(self, directive: Directive) -> bool:
        return isinstance(directive, (Speech, Assassination))

    def lead_in_beats(self) -> set[str]:
        return {"propose_intro", "lady"}

    # ---------------------------------------------------------------- 框架挂钩 / 演出

    def is_opening_anchor(self, directive: Directive) -> bool:
        return isinstance(directive, Deal)  # 录制等"发牌"画面 paint 出来再开抓，对齐视频零点

    def result(self, directive: Directive) -> GameResult | None:
        if isinstance(directive, GameOver):
            return GameResult(winner=directive.winner, win_reason=directive.win_reason)
        return None

    def cues(self, directive: Directive, duration: float) -> list[tuple[str, float]]:
        """一拍 → [(音效名, 拍内偏移秒)]。无音效的拍（主持/发言无内心戏/幕间）返回空。
        与 web/src/stage/sfx.ts 的 cuesFor 手动保持一致——改这里就同步改那边。"""
        match directive:
            case GameOpen():
                return [("open", 0.0)]
            case Deal():
                return []  # 发牌不配音效（"咔咔"声刺耳）——画面照常飞牌，sfx.ts 同此
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

    def summary(self, directive: Directive, scene: BaseScene) -> tuple[str, str, str]:
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
            case VoteReveal():
                return "表决", "magenta", f"{'通过' if directive.approved else '否决'} · 第 {directive.quest}.{directive.attempt} 队"
            case QuestReveal():
                return "任务", "blue", f"第 {directive.quest} 任务 {directive.result}（{directive.fails} 失败）"
            case Assassination():
                return "刺杀", "red", "命中" if directive.hit else "落空"
            case GameOver():
                return "终局", "bold red", f"{directive.winner} 胜 · {directive.win_reason}"
            case Intermission():
                return "幕间", "dim", ""
            case _:
                return "?", "white", type(directive).__name__


def _snip(text: str, n: int = 36) -> str:
    """台词压成一行预览：折叠空白、超长截断（节拍是进度而非全文）。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _seat_name(seats: list[SeatInfo], seat_no: int) -> str:
    return next((s.name for s in seats if s.no == seat_no), f"{seat_no}号")
