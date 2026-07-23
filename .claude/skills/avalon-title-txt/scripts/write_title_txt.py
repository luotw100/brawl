#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


class TitleAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        titles = getattr(namespace, self.dest, None) or []
        titles.append(tuple(values))
        setattr(namespace, self.dest, titles)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write two-line title.txt files for Avalon replay folders.")
    parser.add_argument("root", type=Path, help="Directory containing replay subfolders, or a single replay directory with --single.")
    parser.add_argument("--single", action="store_true", help="Treat root as the replay directory; use --line1 and --line2.")
    parser.add_argument("--line1", help="First cover line for --single mode.")
    parser.add_argument("--line2", help="Second cover line for --single mode.")
    parser.add_argument(
        "--title",
        nargs=3,
        action=TitleAction,
        metavar=("GAME", "LINE1", "LINE2"),
        help="Replay subfolder name plus the two title lines. Repeat for batches.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned writes without modifying files.")
    return parser.parse_args()


def checked_lines(line1: str, line2: str) -> str:
    lines = [line1.strip(), line2.strip()]
    if not all(lines):
        raise SystemExit("title lines must be non-empty")
    if any("\n" in line or "\r" in line for line in lines):
        raise SystemExit("title lines must not contain newlines")
    return "\n".join(lines) + "\n"


def replay_dir(path: Path) -> Path:
    if not path.is_dir():
        raise SystemExit(f"missing replay directory: {path}")
    return path


def main() -> None:
    args = parse_args()
    writes: list[tuple[Path, str]] = []

    if args.single:
        if args.title:
            raise SystemExit("--single cannot be combined with --title")
        if args.line1 is None or args.line2 is None:
            raise SystemExit("--single requires --line1 and --line2")
        writes.append((replay_dir(args.root) / "title.txt", checked_lines(args.line1, args.line2)))
    else:
        if args.line1 is not None or args.line2 is not None:
            raise SystemExit("--line1/--line2 are only valid with --single")
        if not args.title:
            raise SystemExit("provide at least one --title GAME LINE1 LINE2")
        root = replay_dir(args.root)
        for game, line1, line2 in args.title:
            writes.append((replay_dir(root / game) / "title.txt", checked_lines(line1, line2)))

    for path, content in writes:
        if args.dry_run:
            print(f"DRY {path}: {content!r}")
        else:
            path.write_text(content)
            print(f"WROTE {path}")


if __name__ == "__main__":
    main()
