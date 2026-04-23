"""`claude-rotate cleanup` — remove all claude-rotate state from disk.

Deletes ``config_dir`` (accounts, lock), ``cache_dir`` (usage cache), and
``state_dir`` (log.jsonl). Does **not** touch ``~/.claude`` — credentials
that claude itself manages (``~/.claude/.credentials.json``) are out of
scope for this tool.

Destructive — prompts for confirmation unless ``--yes`` is passed.
"""

from __future__ import annotations

import shutil
import sys

from claude_rotate.config import Paths


def execute(paths: Paths, *, assume_yes: bool) -> int:
    targets = [paths.config_dir, paths.cache_dir, paths.state_dir]
    existing = [p for p in targets if p.exists()]

    # Refuse to recurse through symlinks. A hostile user who has write
    # access to the parent could replace e.g. ``~/.config/claude-rotate``
    # with a symlink to ``~`` — ``shutil.rmtree`` would happily delete
    # the target in that case. We only manage regular directories here.
    symlinked = [p for p in existing if p.is_symlink()]
    if symlinked:
        print(
            "  error: one or more target paths are symlinks; refusing to delete:",
            file=sys.stderr,
        )
        for p in symlinked:
            print(f"    - {p} → {p.resolve()}", file=sys.stderr)
        print(
            "  Remove the symlink(s) manually if this is intentional.",
            file=sys.stderr,
        )
        return 1

    if not existing:
        print("  Nothing to clean — no claude-rotate state on disk.", file=sys.stderr)
        return 0

    print("  About to delete:", file=sys.stderr)
    for p in existing:
        print(f"    - {p}", file=sys.stderr)

    if not assume_yes:
        try:
            answer = input("  Proceed? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.", file=sys.stderr)
            return 1
        if answer not in ("y", "yes"):
            print("  Aborted.", file=sys.stderr)
            return 1

    for p in existing:
        shutil.rmtree(p)

    print("  ✓ Removed all claude-rotate state.", file=sys.stderr)
    print(
        "  Run `claude-rotate login <email> <name>` to add accounts again.",
        file=sys.stderr,
    )
    return 0
