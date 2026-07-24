"""``brawl`` 命令入口：顶层子命令路由、玩法装配（板子/演出/resume 登记）与用户错误收口。"""

from __future__ import annotations

import sys
from collections.abc import Callable

from pydantic import ValidationError

from brawl.games import run as games_run
from brawl.games.avalon import rules as avalon_rules
from brawl.games.avalon import run as avalon_run
from brawl.games.avalon.stagecast import STAGE_GAME as AVALON_STAGE
from brawl.games.werewolf import rules as werewolf_rules
from brawl.games.werewolf import run as werewolf_run
from brawl.games.werewolf.stagecast import STAGE_GAME as WEREWOLF_STAGE
from brawl.runtime import console as sr_console
from brawl.runtime import tts as sr_tts
from brawl.runtime.console import ConsoleError
from brawl.runtime.files import UnsafePathError
from brawl.runtime.journal import JournalCorruptionError, JournalInUseError
from brawl.runtime.opencode_runtime import OpenCodeRuntimeError
from brawl.runtime.tts import TTSError
from brawl.stage import cover as stage_cover
from brawl.stage import record as stage_record
from brawl.stage import stagecast as stage_registry
from brawl.voicelab import forge as voice_forge
from brawl.voicelab.forge import SettingsError as VoicelabSettingsError

# 装配根：runtime/stage/games 壳都不 import 玩法包，玩法能力（开局板子、resume、演出投影）
# 的登记只能发生在这里。
stage_registry.register(AVALON_STAGE)
stage_registry.register(WEREWOLF_STAGE)
_AVALON_BOARD = avalon_rules.MERLIN_ASSASSIN_PERCIVAL_MORGANA_MORDRED_OBERON_LADY_10
_WEREWOLF_BOARD = werewolf_rules.PRE_WITCH_HUNTER_IDIOT_12
games_run.register_board(
    games_run.BoardEntry(id=_AVALON_BOARD.id, game="avalon", players=_AVALON_BOARD.players, title=_AVALON_BOARD.name, run=avalon_run.run_game)
)
games_run.register_board(
    games_run.BoardEntry(id=_WEREWOLF_BOARD.id, game="werewolf", players=_WEREWOLF_BOARD.players, title=_WEREWOLF_BOARD.name, run=werewolf_run.run_game)
)
games_run.register_resumer("avalon", avalon_run.resume_game)
games_run.register_resumer("werewolf", werewolf_run.resume_game)


def _exit_on_nonzero(main: Callable[[list[str]], int]) -> Callable[[list[str]], None]:
    def run(argv: list[str]) -> None:
        if code := main(argv):
            raise SystemExit(code)

    return run


COMMANDS: dict[str, Callable[[list[str]], None]] = {
    "run": _exit_on_nonzero(games_run.main),
    "resume": _exit_on_nonzero(games_run.resume_main),
    "console": sr_console.main,
    "tts": sr_tts.main,
    "voice-forge": voice_forge.main,
    "record": _exit_on_nonzero(stage_record.main),
    "cover": _exit_on_nonzero(stage_cover.main),
}

_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("开局", (
        ("run", "开一局（--board 选板子）"),
        ("resume", "续跑中断的一局"),
    )),
    ("回放与出品", (
        ("console", "在终端回放一局（打字机效果）"),
        ("tts", "为 replay 生成配音"),
        ("record", "把 replay 录成视频"),
        ("cover", "生成封面图"),
    )),
    ("工具", (
        ("voice-forge", "锻造音色"),
    )),
)


def _usage() -> str:
    lines = ["brawl —— AI 桌游对局生成与舞台出品", ""]
    for group, commands in _GROUPS:
        lines.append(f"{group}:")
        for name, blurb in commands:
            lines.append(f"  {name:<11} {blurb}")
        lines.append("")
    lines.append("brawl <命令> -h 查看该命令的参数")
    return "\n".join(lines)


def _validation_message(exc: ValidationError) -> str:
    """pydantic 错误 → 单行中文（去掉 "Value error, " 前缀，带上字段定位）。"""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}：{msg}" if loc else msg)
    return "；".join(parts)


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return 0 if argv else 1
    command, rest = argv[0], argv[1:]
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"未知命令: {command}\n\n{_usage()}", file=sys.stderr)
        return 2
    try:
        handler(rest)
    except (
        games_run.SettingsError,
        VoicelabSettingsError,
        ConsoleError,
        FileExistsError,
        JournalCorruptionError,
        JournalInUseError,
        OpenCodeRuntimeError,
        TTSError,
        UnsafePathError,
    ) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"配置错误: {_validation_message(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    return 0
