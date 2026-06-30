"""玩法注册表：meta.game → GamePlugin。引擎据此为每一局挑对应玩法，自身零玩法知识。

加玩法 = 在 games/<game>/ 写好 plugin，在 `_PLUGINS` 登记一行。`DEFAULT_GAME` 是待机/冷启动
底图用的玩法（直播频道当前以阿瓦隆为主），也是 meta 无 game 字段的老 replay 的回退。
"""

from __future__ import annotations

from pathlib import Path

from server import replay
from server.games.avalon.plugin import AvalonPlugin
from server.games.base import GamePlugin

DEFAULT_GAME = "avalon"

_PLUGINS: dict[str, GamePlugin] = {
    "avalon": AvalonPlugin(),
}


def plugin_for(game_id: str) -> GamePlugin:
    """按玩法标识取插件。未登记的玩法直接抛——契约漂移（meta 写了个没人接的玩法）要立刻暴露。"""
    try:
        return _PLUGINS[game_id]
    except KeyError:
        raise ValueError(f"未登记的玩法：{game_id}（registry._PLUGINS 要补一行）") from None


def plugin_for_replay(replay_dir: Path) -> GamePlugin:
    """读这局的 meta.game 取插件（老 avalon replay 无此键 → 回退 DEFAULT_GAME）。"""
    return plugin_for(replay.load_game_id(replay_dir))


def default_plugin() -> GamePlugin:
    """待机/冷启动底图用的默认玩法插件。"""
    return plugin_for(DEFAULT_GAME)
