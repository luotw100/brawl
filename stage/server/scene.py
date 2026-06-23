"""阿瓦隆 adapter：把 replay 的 god-view 事件翻成舞台指令，并把指令折叠成场景快照。

这是引擎里唯一懂"阿瓦隆"的地方——engine/hub/overlay 全是游戏无关的。将来加玩法 =
并列一个 adapter（adapt + apply），引擎零分叉。

v2"电视信号"语义下 `apply` 是**全系统唯一的 reducer**：engine 每拍 adapt → apply 折进
Scene → 把完整 Scene 广播出去；客户端是纯渲染函数，不持有状态、不做折叠。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from server.protocol import (
    Assassination,
    Deal,
    Directive,
    FeedItem,
    GameOpen,
    GameOver,
    HostSpeech,
    Intermission,
    ProposalLog,
    QuestCard,
    QuestLog,
    QuestReveal,
    Scene,
    SeatInfo,
    SeatVote,
    Speech,
    VoteReveal,
)

FEED_CAP = 5  # 服务端只留最近几拍（窗口旋钮 1~3 再从中裁剪下发）


def load_names(replay_dir: Path) -> dict[str, str]:
    """边界：meta.setup 的 cast/names 两列 → persona id 到显示名的映射。

    显示名在生成时已快照进各局 meta（与 voices 同源），回放层据此自圆、不再读 games 的
    cast.yaml——一局 replay 目录就是自包含的。缺 meta/names 就空表：座位回退裸 persona id
    （老 replay 的优雅降级，不是错误）。"""
    setup = _setup(replay_dir)
    return dict(zip(setup.get("cast", []), setup.get("names", []), strict=False))


def _setup(replay_dir: Path) -> dict[str, Any]:
    """读 meta.json 的 setup 段；没有 meta（老 replay）就空 dict。"""
    meta = replay_dir / "meta.json"
    if not meta.is_file():
        return {}
    return json.loads(meta.read_text(encoding="utf-8")).get("setup", {})


def load_model_labels(replay_dir: Path) -> dict[str, str]:
    """逻辑模型名 → 对外展示名（meta.model_labels，录制时按 models.yaml 的 display 烧入）。

    座位事件带的是逻辑名（如 oc-deepseek-v4-pro），stage 据此查这表显成 "DeepSeek v4 Pro"
    （与 load_names 同套路：自包含、不读 games 配置）。缺 meta / 老 replay 无此键 → 空表，
    座位不显模型名（优雅降级）。"""
    meta = replay_dir / "meta.json"
    if not meta.is_file():
        return {}
    return json.loads(meta.read_text(encoding="utf-8")).get("model_labels", {})


def load_events(replay_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (replay_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def load_tts(replay_dir: Path) -> dict[str, dict[str, Any]]:
    """离线 TTS 口子：tts/manifest.json（call_id → {file, duration_ms, ...}）。

    一份 manifest 同时供两用——音频 URL（file 字段）与节奏时长（duration_ms）。
    没有 manifest 就空表：发言回退纯字数估算、不带音频（无声照常播，是有效状态不是错误）。
    """
    path = replay_dir / "tts" / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _audio_url(replay_id: str, call_id: str | None, tts: dict[str, dict[str, Any]]) -> str | None:
    """manifest 里有这条 call_id 才给 URL（manifest 是音频存在性的唯一真相，不再 stat 文件系统）。"""
    entry = tts.get(call_id or "")
    return f"/replays/{replay_id}/tts/{entry['file']}" if entry else None


def avatar_urls(replay_id: str, replay_dir: Path) -> dict[int, str]:
    """各座位的厂商头像 URL（avatars/<座位号>.png → /replays/<id>/avatars/<file>）。

    录制时按座位把厂商 logo 烧进 avatars/；这个目录就是头像存在性的唯一真相（不另读 meta）：
    烧入了就有、没有就缺。缺则前端回退 hash 色块。没有 avatars/ 目录（老 replay）→ 空表。"""
    avatars = replay_dir / "avatars"
    if not avatars.is_dir():
        return {}
    return {int(f.stem): f"/replays/{replay_id}/avatars/{f.name}"
            for f in avatars.iterdir() if f.is_file() and f.stem.isdigit()}


def audio_durations(replay_id: str, tts: dict[str, dict[str, Any]]) -> dict[str, float]:
    """audio_url → 时长（秒）。键就是指令上的 audio_url（复用 _audio_url 同一套构造）：
    引擎据此让一拍停满音频。刻意不让引擎自己从 raw event 捞 call_id——多数事件一拍一次
    调用、call_id 和音频键一致，但仙女一拍压两次调用（choose 私下决策 + announce 口播），
    音频键是 announce_call_id、并无顶层 call_id。把"播哪条音频"统一收到 adapt 拍板的
    audio_url 上，引擎按同一条音频取时长，divergence 从结构上消除。"""
    return {url: e["duration_ms"] / 1000 for cid, e in tts.items()
            if (url := _audio_url(replay_id, cid, tts)) is not None}


# 每条公开发言事件的 (call_id 字段, 文本字段)。与 games/avalon/tts.py 的 _utterances 同一套
# 名单：那边合成谁、进库就核对谁，多一个少一个都会让"配音齐了没"判错。game_end 不配音故不在内。
_VOICED = {
    "host": ("call_id", "text"),
    "propose": ("call_id", "speech"),
    "discuss": ("call_id", "text"),
    "lady": ("announce_call_id", "speech"),
    "assassination": ("call_id", "speech"),
    "banter": ("call_id", "text"),
}


def is_broadcastable(replay_dir: Path) -> bool:
    """这局能否进直播库：唯一闸是【对局跑完】——events 里有 game_end（游戏逻辑只在真·终局发的，
    比"没有 state.sqlite"强：半截崩溃同样没 game_end，一并拦住）。

    TTS 柔性、不设闸：有配音就播、没有/缺张就静默（adapt 里 _audio_url 查不到就不带音频，
    一拍节奏回退字数估算）——无声是有效状态，方便不烧 TTS 直接测画面。

    残缺/损坏一律判否：这是边界过滤，library 拿到的都是真能播的，扫描自身绝不因坏目录崩。"""
    try:
        events = load_events(replay_dir)
        return any(e.get("type") == "game_end" for e in events)
    except (OSError, json.JSONDecodeError, KeyError):
        return False


def load_host_name(replay_dir: Path) -> str:
    """meta.setup.host_name → 主持人显示名（生成时快照进 meta，与玩家显示名同源）。
    老 replay 没有 host 事件、这名字不会被用到；缺 meta/host_name 都退 '主持人'。"""
    return _setup(replay_dir).get("host_name") or "主持人"


def adapt(event: dict[str, Any], *, replay_id: str, tts: dict[str, dict[str, Any]], names: dict[str, str],
          host_name: str = "主持人", avatars: dict[int, str] | None = None,
          model_labels: dict[str, str] | None = None) -> Directive | None:
    """一条 replay 事件 → 一条舞台指令。未知事件类型直接抛——契约变了要立刻暴露，不静默吞。"""
    avatars = avatars or {}
    model_labels = model_labels or {}
    t = event["type"]
    if t == "host":
        if event["beat"] == "reveal":
            return None  # 上帝视角全程亮身份，主持人复述身份纯属多余——这一拍不演（其 TTS 也随之不播）
        return HostSpeech(
            name=host_name, beat=event["beat"], text=event["text"],
            audio_url=_audio_url(replay_id, event.get("call_id"), tts),
        )
    if t == "game_start":
        return GameOpen(
            players=event["players"], good=event["good"], evil=event["evil"],
            seats=[_seat(s, names, avatars, model_labels) for s in event.get("seats", [])],  # 老 replay 无 seats → 空
        )
    if t == "deal":
        return Deal(
            seats=[_seat(s, names, avatars, model_labels) for s in event["seats"]],
            leader_no=event["leader_no"],
            lady_holder=event["lady_holder"],
        )
    if t == "propose":
        return Speech(
            phase="propose", seat_no=event["leader_no"], text=event["speech"], monologue=event["monologue"],
            quest=event["quest"], attempt=event["attempt"], team=event["team"],
            audio_url=_audio_url(replay_id, event.get("call_id"), tts),
        )
    if t == "discuss":
        return Speech(
            phase="discuss", seat_no=event["seat_no"], text=event["text"], monologue=event["monologue"],
            quest=event["quest"], attempt=event["attempt"],
            audio_url=_audio_url(replay_id, event.get("call_id"), tts),
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
            audio_url=_audio_url(replay_id, event.get("announce_call_id"), tts),
        )
    if t == "assassination":
        return Assassination(
            assassin_no=event["assassin_no"], target_no=event["target_no"], hit=event["hit"],
            text=event["speech"], monologue=event["monologue"],
            audio_url=_audio_url(replay_id, event.get("call_id"), tts),
        )
    if t == "game_end":
        return GameOver(
            winner=event["winner"], win_reason=event["win_reason"],
            seats=[_seat(s, names, avatars, model_labels) for s in event["seats"]],
        )
    if t == "banter":
        return Speech(
            phase="banter", seat_no=event["seat_no"], text=event["text"], monologue=event["monologue"],
            audio_url=_audio_url(replay_id, event.get("call_id"), tts),
        )
    raise ValueError(f"未知事件类型：{t}（events 契约变了，adapter 要跟上）")


def _seat(s: dict[str, Any], names: dict[str, str], avatars: dict[int, str],
          model_labels: dict[str, str]) -> SeatInfo:
    return SeatInfo(
        no=s["no"], name=names.get(s["persona"], s["persona"]), persona=s["persona"],
        role=s["role"], side=s["side"], model=s.get("model", ""),
        avatar_url=avatars.get(s["no"]),
        model_display=model_labels.get(s.get("model") or ""),
    )


def apply(scene: Scene, item: FeedItem) -> None:
    """把一拍折进舞台状态（这是全系统唯一的 reducer——客户端是纯渲染，不折叠）。"""
    directive = item.directive
    scene.feed.append(item)
    del scene.feed[:-FEED_CAP]
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
            pass  # 主持人不占座位：speaking 已清空，台词只进 feed
        case Intermission():
            scene.speaking = None
