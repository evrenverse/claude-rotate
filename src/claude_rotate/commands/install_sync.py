"""`claude-rotate install-sync` — install a crontab entry for periodic sync.

Idempotent. Safe to run multiple times. Uses a marker comment
(``# [claude-rotate:sync]``) to identify our own line, which means we
can replace or remove it without touching other user entries.

Usage:
  claude-rotate install-sync             # install / update
  claude-rotate install-sync --uninstall # remove
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from claude_rotate.config import Paths

CRON_TAG = "# [claude-rotate:sync]"
CRON_SCHEDULE = "*/2 * * * *"


def build_cron_line(binary: str, state_dir: Path) -> str:
    """Build the crontab line for the sync job, with our marker at end."""
    log = state_dir / "sync.log"
    return f"{CRON_SCHEDULE} {binary} sync-credentials >>{log} 2>&1  {CRON_TAG}"


def merge_crontab(existing: str, new_line: str, *, remove: bool) -> tuple[str, bool]:
    """Merge new_line into existing crontab text.

    Returns (merged_text, changed).
    - If remove=True: strip any line carrying CRON_TAG, ignore new_line.
    - Else: replace any existing CRON_TAG line with new_line, or append
      if absent. No-op if an identical tagged line is already present.
    """
    lines = existing.splitlines()
    kept: list[str] = [line for line in lines if CRON_TAG not in line]
    had_tag = len(kept) != len(lines)

    if remove:
        if not had_tag:
            return existing, False
        return "\n".join(kept) + ("\n" if kept else ""), True

    # install / replace
    if had_tag and new_line in lines:
        # already present verbatim → no-op
        return existing, False
    kept.append(new_line)
    return "\n".join(kept) + "\n", True


def execute(paths: Paths, *, uninstall: bool) -> int:
    binary = shutil.which("claude-rotate")
    if not binary:
        print("error: claude-rotate is not on PATH", file=sys.stderr)
        return 1

    paths.state_dir.mkdir(parents=True, exist_ok=True)
    new_line = build_cron_line(binary, paths.state_dir)

    # Read current crontab — exit 1 when the user has none (no crontab)
    read = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    existing = read.stdout if read.returncode == 0 else ""

    merged, changed = merge_crontab(existing, new_line, remove=uninstall)
    if not changed:
        print("sync cron entry already up to date — nothing to do", file=sys.stderr)
        return 0

    write = subprocess.run(
        ["crontab", "-"],
        input=merged,
        capture_output=True,
        text=True,
        check=False,
    )
    if write.returncode != 0:
        print(f"error: crontab write failed: {write.stderr.strip()}", file=sys.stderr)
        return 1

    action = "removed" if uninstall else "installed"
    print(f"sync cron entry {action} ({CRON_SCHEDULE})", file=sys.stderr)
    return 0
