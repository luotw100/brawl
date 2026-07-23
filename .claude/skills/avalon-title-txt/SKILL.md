---
name: avalon-title-txt
description: Generate and write two-line title.txt cover titles for Avalon replay/resource folders. Use when Codex is asked to watch or summarize AvalonGames/Avalon replay folders, polish short-video cover titles, create title.txt files, or produce white/yellow two-part Chinese cover text for Avalon game videos.
---

# Avalon Title.txt

## Goal

Create `title.txt` for each Avalon replay/resource folder. Each file must contain exactly two non-empty lines:

```text
封面白字
封面黄字
```

The first line sets up the situation; the second line delivers the twist, punchline, or outcome. These lines are intended for video cover art where line 1 is white and line 2 is yellow.

## Workflow

1. Identify replay folders. A replay folder usually contains `events.jsonl`, `meta.json`, `state.sqlite`, optionally `video.mp4`, and may already contain `title.txt`.
2. Read structured replay facts before inventing titles. Prefer `events.jsonl` and `meta.json`; use video only when the user explicitly asks to inspect visuals or when records are missing.
3. Extract the title-worthy conflict:
   - final winner and win reason
   - task trajectory and decisive failed/successful quest
   - assassination target and whether it hit
   - Lady of the Lake announcements and lies
   - hidden role irony: Merlin hiding, Percival misreading, Mordred invisible, Oberon drifting, Morgana impersonating
   - postgame `banter` lines when present, because they often name the real turning point
4. Draft one or more two-line title candidates per game. Keep them concrete, short, and cover-friendly.
5. Write the selected title with `scripts/write_title_txt.py`, or manually if only one file is involved.
6. Verify each `title.txt` has exactly two non-empty lines and no labels such as `标题：`, `白字：`, or `黄字：`.

## Title Style

Use compact Chinese cover copy, not a full sentence summary.

Good patterns:

```text
梅林全程装死
刺客一刀砍偏
```

```text
三红互发金水
蓝方全桌信了
```

```text
莫甘娜卖队友
莫德雷德藏到最后
```

Prefer:

- 6-12 Chinese characters per line when possible; 13-16 is acceptable for role-heavy twists.
- Specific roles and actions over generic words like “精彩对局”.
- Two-part rhythm: setup + reversal, tactic + consequence, mistake + punishment.
- Accurate spoilers when the user is making cover titles for posted videos; do not hide the hook if the existing style uses role spoilers.

Avoid:

- Full-width punctuation unless it materially improves the cover.
- Hashtags, intro, description, tags, markdown, numbering, or blank lines inside `title.txt`.
- Claims not supported by replay facts.
- Overly similar titles across a batch; each title should identify that game’s unique drama.

## Batch Writing Script

Use the bundled script to reduce file-writing mistakes:

```bash
python3 /home/luotw/code/brawl/.agents/skills/avalon-title-txt/scripts/write_title_txt.py /path/to/AvalonGames \
  --title 001 梅林全程装死 刺客一刀砍偏 \
  --title 002 三红互发金水 蓝方全桌信了
```

The script writes `/path/to/AvalonGames/<game>/title.txt` for each `--title GAME LINE1 LINE2`. It fails if a target directory does not exist.

Use `--dry-run` before writing when the batch is large or the user has not confirmed final wording.
