"""replay 通用读取器：从一局 replay 目录里取出**与玩法无关**的随局事实。

边界纪律：一局 replay 目录就是自包含的——显示名/音色/头像/对外模型名等身份信息都在生成时
快照进各局 meta.json 与产物目录，回放层据此自圆，不再读 games 的 cast.yaml。这里全是纯读取，
任何玩法通用（事件流、TTS 清单与时长、座位头像、显示名、主持名、进库闸）；只有"怎么把事件翻成
指令"才是玩法专属，落在 games/<game>/plugin.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class LibraryItem:
    """导播台片库条目：id 是相对 replay_root 的稳定播放键，game/tags 是运营侧标签。"""

    id: str
    game: str
    tags: list[str]


def load_game_id(replay_dir: Path) -> str:
    """这局是哪个玩法：meta.game；缺省（老 avalon replay 无此键）退 'avalon'。registry 据此派发 plugin。"""
    return _meta(replay_dir).get("game") or "avalon"


def load_events(replay_dir: Path) -> list[dict[str, Any]]:
    """读一局的事件流。旧格式 replay 自带 events.jsonl 直接读；新格式（sentence runtime）
    只有 sentences.jsonl，经 games/sentences.py 的桥翻译成同一套事件契约。"""
    events_path = replay_dir / "events.jsonl"
    if events_path.is_file():
        return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    sent_path = replay_dir / "sentences.jsonl"
    if sent_path.is_file():
        from server.games import sentences as sentences_bridge

        return sentences_bridge.events_from_sentences(replay_dir, load_game_id(replay_dir))
    raise FileNotFoundError(f"{replay_dir} 里既没有 events.jsonl 也没有 sentences.jsonl")


def load_tts(replay_dir: Path) -> dict[str, dict[str, Any]]:
    """离线 TTS 口子：tts/manifest.json（call_id → {file, duration_ms, ...}）。

    一份 manifest 同时供两用——音频 URL（file 字段）与节奏时长（duration_ms）。
    没有 manifest 就空表：发言回退纯字数估算、不带音频（无声照常播，是有效状态不是错误）。
    """
    path = replay_dir / "tts" / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def audio_url(replay_id: str, call_id: str | None, tts: dict[str, dict[str, Any]]) -> str | None:
    """manifest 里有这条 call_id 才给 URL（manifest 是音频存在性的唯一真相，不再 stat 文件系统）。"""
    entry = tts.get(call_id or "")
    return f"/replays/{_url_path(replay_id)}/tts/{quote(entry['file'])}" if entry else None


def audio_durations(replay_id: str, tts: dict[str, dict[str, Any]]) -> dict[str, float]:
    """audio_url → 时长（秒）。键就是指令上的 audio_url（复用 audio_url 同一套构造）：引擎据此让一拍
    停满音频。刻意不让引擎自己从 raw event 捞 call_id——多数事件一拍一次调用、call_id 和音频键一致，
    但仙女事件含 choose 私下决策与 announce 公开发言，音频键是 announce_call_id、并无顶层 call_id。
    把"播哪条音频"统一收到 adapt 拍板的 audio_url 上，引擎按同一条音频取时长，divergence 从结构上消除。"""
    return {url: e["duration_ms"] / 1000 for cid, e in tts.items()
            if (url := audio_url(replay_id, cid, tts)) is not None}


def avatar_urls(replay_id: str, replay_dir: Path) -> dict[int, str]:
    """各座位的厂商头像 URL（avatars/<座位号>.png → /replays/<id>/avatars/<file>）。

    录制时按座位把厂商 logo 烧进 avatars/；这个目录就是头像存在性的唯一真相（不另读 meta）：
    烧入了就有、没有就缺。缺则前端回退 hash 色块。没有 avatars/ 目录（老 replay）→ 空表。"""
    avatars = replay_dir / "avatars"
    if not avatars.is_dir():
        return {}
    return {int(f.stem): f"/replays/{_url_path(replay_id)}/avatars/{quote(f.name)}"
            for f in avatars.iterdir() if f.is_file() and f.stem.isdigit()}


def library(root: Path) -> list[LibraryItem]:
    """递归发现 replay_root 下所有可播 replay，并补齐导播台标签。

    replay id 统一用 POSIX 相对路径；播放、排队、音频 URL 都走同一个 id，因此 Docker 只挂
    `~/Downloads` 也能发现 `AvalonGames/001`、`WerewolfGames/draft001` 这类二级目录。
    """
    items = [
        LibraryItem(id=_replay_id(root, d), game=load_game_id(d), tags=_tags(d))
        for d in _replay_dirs(root)
        if is_broadcastable(d)
    ]
    return sorted(items, key=lambda item: item.id)


def _replay_dirs(root: Path):
    """Find replay roots without descending into managed OpenCode state."""
    for directory, dirnames, filenames in root.walk():
        dirnames[:] = [name for name in dirnames if name != "_opencode"]
        if "events.jsonl" in filenames or "sentences.jsonl" in filenames:
            yield directory


def load_names(replay_dir: Path) -> dict[str, str]:
    """边界：meta.setup 的 cast/names 两列 → persona id 到显示名的映射。

    显示名在生成时已快照进各局 meta（与 voices 同源），回放层据此自圆。缺 meta/names 就空表：
    座位回退裸 persona id（老 replay 的优雅降级，不是错误）。"""
    setup = _setup(replay_dir)
    return dict(zip(setup.get("cast", []), setup.get("names", []), strict=False))


def load_host_name(replay_dir: Path) -> str:
    """meta.setup.host_name → 主持人显示名（生成时快照进 meta，与玩家显示名同源）。
    老 replay 没有 host 事件、这名字不会被用到；缺 meta/host_name 都退 '主持人'。"""
    return _setup(replay_dir).get("host_name") or "主持人"


def load_model_labels(replay_dir: Path) -> dict[str, str]:
    """逻辑模型名 → 对外展示名（meta.model_labels，录制时按 models.yaml 的 display 烧入）。

    座位事件带的是逻辑名（如 oc-deepseek-v4-pro），stage 据此查这表显成 "DeepSeek v4 Pro"
    （与 load_names 同套路：自包含、不读 games 配置）。缺 meta / 老 replay 无此键 → 空表，
    座位不显模型名（优雅降级）。"""
    return _meta(replay_dir).get("model_labels", {})


def is_broadcastable(replay_dir: Path) -> bool:
    """这局能否进直播库：唯一闸是【对局跑完】——events 里有 game_end（游戏逻辑只在真·终局发的，
    比"没有 state.sqlite"强：半截崩溃同样没 game_end，一并拦住。avalon/werewolf 都以 game_end 收尾）。

    TTS 柔性、不设闸：有配音就播、没有/缺张就静默（adapt 里查不到就不带音频，一拍节奏回退字数估算）
    ——无声是有效状态，方便不烧 TTS 直接测画面。

    残缺/损坏一律判否：这是边界过滤，library 拿到的都是真能播的，扫描自身绝不因坏目录崩。"""
    try:
        events = load_events(replay_dir)
        return any(e.get("type") == "game_end" for e in events)
    except (OSError, json.JSONDecodeError, KeyError):
        return False


def _tags(replay_dir: Path) -> list[str]:
    tts = load_tts(replay_dir)
    tags = ["TTS" if tts else "无TTS"]
    if replay_dir.joinpath("video.mp4").is_file():
        tags.append("视频")
    if replay_dir.joinpath("covers").is_dir():
        tags.append("封面")
    if replay_dir.joinpath("title.txt").is_file():
        tags.append("标题")
    return tags


def _replay_id(root: Path, replay_dir: Path) -> str:
    return replay_dir.relative_to(root).as_posix()


def _url_path(value: str) -> str:
    return quote(value, safe="/")


def _meta(replay_dir: Path) -> dict[str, Any]:
    """读 meta.json；没有 meta（老 replay）就空 dict。"""
    meta = replay_dir / "meta.json"
    if not meta.is_file():
        return {}
    return json.loads(meta.read_text(encoding="utf-8"))


def _setup(replay_dir: Path) -> dict[str, Any]:
    """读 meta.json 的 setup 段；没有 meta（老 replay）就空 dict。"""
    return _meta(replay_dir).get("setup", {})
