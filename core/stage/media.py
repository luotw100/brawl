"""ReplayMediaResolver：把 replay 目录里的媒体事实解析成 URL/路径/时长。

这是"语义键 → 传输事实"的唯一解析点，游戏盲：
- `audio(call_id)`：tts/manifest.json（音频存在性的唯一真相）→ (url, 秒)
- `avatar(seat)`：avatars/<座位号>.*（头像存在性的唯一真相）→ url
- `audio_path(url)`：录制拼音轨用——resolver 自己持有 url→绝对路径 的正向映射，
  不做 URL 字符串反解（旧 record 剥 /replays/ 前缀的隐患从结构上消除）。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote


class ReplayMediaResolver:
    def __init__(self, replay_dir: Path, *, url_prefix: str = "/replay") -> None:
        self._dir = replay_dir
        self._prefix = url_prefix.rstrip("/")
        self._manifest = self._load_manifest(replay_dir / "tts" / "manifest.json")
        self._by_url: dict[str, tuple[Path, float]] = {
            self._tts_url(entry["file"]): (replay_dir / "tts" / entry["file"], entry["duration_ms"] / 1000)
            for entry in self._manifest.values()
        }

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, dict]:
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _tts_url(self, file: str) -> str:
        return f"{self._prefix}/tts/{quote(file)}"

    def audio(self, call_id: str) -> tuple[str, float] | None:
        entry = self._manifest.get(call_id)
        if entry is None:
            return None
        return self._tts_url(entry["file"]), entry["duration_ms"] / 1000

    def avatar(self, seat: int) -> str | None:
        avatars = self._dir / "avatars"
        if not avatars.is_dir():
            return None
        for f in avatars.iterdir():
            if f.is_file() and f.stem == str(seat):
                return f"{self._prefix}/avatars/{quote(f.name)}"
        return None

    def durations(self) -> dict[str, float]:
        """audio_url → 秒（playplan 的节奏时长表，与 URL 构造同源）。"""
        return {url: seconds for url, (_, seconds) in self._by_url.items()}

    def audio_path(self, url: str) -> Path:
        """audio_url → 磁盘绝对路径（录制离线拼音轨；url 必须是本 resolver 产出的）。"""
        try:
            return self._by_url[url][0]
        except KeyError:
            raise ValueError(f"audio_url 不是本 replay 的媒体：{url}") from None
