"""``brawl`` 命令入口：只做顶层子命令路由与用户错误收口。"""

from __future__ import annotations

import sys
from collections.abc import Callable

from pydantic import ValidationError

from avalon import run as avalon_run
from avalon.settings import SettingsError as AvalonSettingsError
from avalon.stagecast import STAGE_GAME as AVALON_STAGE
from core.runtime import console as sr_console
from core.runtime import tts as sr_tts
from core.runtime.console import ConsoleError
from core.runtime.files import UnsafePathError
from core.runtime.journal import JournalCorruptionError, JournalInUseError
from core.runtime.opencode_runtime import OpenCodeRuntimeError
from core.runtime.tts import TTSError
from core.stage import cover as stage_cover
from core.stage import record as stage_record
from core.stage import stagecast as stage_registry
from voicelab import forge as voice_forge
from voicelab.forge import SettingsError as VoicelabSettingsError
from werewolf import run as werewolf_run
from werewolf.settings import SettingsError as WerewolfSettingsError
from werewolf.stagecast import STAGE_GAME as WEREWOLF_STAGE

# 装配根：core 不 import 玩法包，演出玩法的登记只能发生在这里。
stage_registry.register(AVALON_STAGE)
stage_registry.register(WEREWOLF_STAGE)


def _exit_on_nonzero(main: Callable[[list[str]], int]) -> Callable[[list[str]], None]:
    def run(argv: list[str]) -> None:
        if code := main(argv):
            raise SystemExit(code)

    return run


COMMANDS: dict[str, Callable[[list[str]], None]] = {
    "avalon": avalon_run.main,
    "werewolf": werewolf_run.main,
    "console": sr_console.main,
    "tts": sr_tts.main,
    "voice-forge": voice_forge.main,
    "record": _exit_on_nonzero(stage_record.main),
    "cover": _exit_on_nonzero(stage_cover.main),
}


def _usage() -> str:
    return f"用法: brawl <命令> [参数...]\n可用命令: {', '.join(sorted(COMMANDS))}\n各命令参数见: brawl <命令> --help"


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
        AvalonSettingsError,
        WerewolfSettingsError,
        VoicelabSettingsError,
        ConsoleError,
        FileExistsError,
        JournalCorruptionError,
        JournalInUseError,
        OpenCodeRuntimeError,
        TTSError,
        UnsafePathError,
        ValidationError,
    ) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    return 0


__all__ = ["COMMANDS", "main"]
