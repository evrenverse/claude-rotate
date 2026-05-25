"""Lightweight entry point for Claude Code hook commands."""

from __future__ import annotations

import sys

from claude_rotate.commands.hook import execute
from claude_rotate.config import ensure_dirs, paths


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: claude-rotate-hook <session-start|user-prompt-submit>", file=sys.stderr)
        return 2
    resolved_paths = paths()
    ensure_dirs(resolved_paths)
    return execute(resolved_paths, args[0])


if __name__ == "__main__":
    raise SystemExit(main())
