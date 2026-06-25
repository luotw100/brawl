"""brawl 命令入口:纯子命令路由。

只负责认出是哪个玩法子命令,然后把**剩余参数原样**转给那个玩法自己的入口。
这里不认识任何玩法的具体参数——每个玩法的配置由它自己的 settings.py 唯一定义
(settings 即 CLI,flag 自动派生)、自己的 run.main 解析。加玩法只往 GAMES 登记一行。
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from pydantic import ValidationError

from avalon import replay as avalon_replay
from avalon import run as avalon_run
from avalon import title as avalon_title
from avalon import tts as avalon_tts
from avalon.settings import SettingsError as AvalonSettingsError
from avalon.title import SettingsError as AvalonTitleError
from avalon.tts import SettingsError as AvalonTtsError
from llmview import show as llm_view
from llmview.show import ViewError
from undercover import run as undercover_run
from undercover.settings import SettingsError as UndercoverSettingsError
from voicelab import forge as voice_forge
from voicelab.forge import SettingsError as VoicelabSettingsError

# 子命令名(含中文别名)→ 该玩法的 main(argv) 入口
GAMES: dict[str, Callable[[list[str]], None]] = {
    "undercover": undercover_run.main,
    "avalon": avalon_run.main,
    "avalon-replay": avalon_replay.main,  # 回放一局 replay 快照（非生成，纯消费 events.jsonl）
    "avalon-tts": avalon_tts.main,  # 把一局公开发言合成音频，落 replay/tts/（柔性续合）
    "avalon-title": avalon_title.main,  # 用 LLM 总结一局发布标题/简介/标签，落 replay/title.txt
    "voice-forge": voice_forge.main,  # 音色铸造：嗓音描述 → 阿里云 voice_id + 试听 wav（跨玩法）
    "chat-view": llm_view.main,  # 把 llm_calls.jsonl 第 N 行渲染成聊天记录（跨玩法）
}


def _usage() -> str:
    names = ", ".join(sorted({n for n in GAMES if n.isascii()}))
    return f"用法: brawl <游戏> [参数...]\n可用游戏: {names}\n各游戏参数见: brawl <游戏> --help"


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return 0 if argv else 1
    game, rest = argv[0], argv[1:]
    handler = GAMES.get(game)
    if handler is None:
        print(f"未知游戏: {game}\n\n{_usage()}", file=sys.stderr)
        return 2
    try:
        handler(rest)
    except (UndercoverSettingsError, AvalonSettingsError, AvalonTtsError, AvalonTitleError, VoicelabSettingsError, ViewError, ValidationError) as e:
        # 用户配置错误(坏的 --config、字段校验失败):只打印消息,不甩 traceback。
        print(f"配置错误: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
