"""`claude-rotate install-sync` — install crontab entries for periodic sync.

Two entries are installed:

- ``*/2 * * * *`` — keeps tokens warm during long idle periods (tokens
  refresh every ~4h, cron notices within 2 min).
- ``@reboot`` (with a short sleep so the network stack is up) — covers
  the case where the user starts ``claude`` seconds after boot, before
  the periodic schedule has had a chance to fire.

Idempotent. Safe to run multiple times. Uses a marker comment
(``# [claude-rotate:sync]``) on every line we own so we can replace or
remove them without touching other user entries.

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
CRON_SCHEDULE_PERIODIC = "*/2 * * * *"
CRON_SCHEDULE_REBOOT = "@reboot"
# Delay after boot before the first refresh — lets the network stack
# come up. 30s is comfortable on consumer hardware; CI/container images
# typically have the network ready before cron even starts.
REBOOT_DELAY_SECONDS = 30


def build_cron_lines(binary: str, state_dir: Path) -> list[str]:
    """Build the crontab lines for the sync jobs, with markers at end."""
    log = state_dir / "sync.log"
    periodic = f"{CRON_SCHEDULE_PERIODIC} {binary} sync-credentials >>{log} 2>&1  {CRON_TAG}"
    reboot = (
        f"{CRON_SCHEDULE_REBOOT} sleep {REBOOT_DELAY_SECONDS} && "
        f"{binary} sync-credentials >>{log} 2>&1  {CRON_TAG}"
    )
    return [periodic, reboot]


def merge_crontab(existing: str, new_lines: list[str], *, remove: bool) -> tuple[str, bool]:
    """Merge new_lines into existing crontab text.

    Returns (merged_text, changed).
    - If remove=True: strip any line carrying CRON_TAG, ignore new_lines.
    - Else: drop every existing CRON_TAG line, then append all new_lines.
      No-op if the tagged lines already match new_lines exactly.
    """
    lines = existing.splitlines()
    kept: list[str] = [line for line in lines if CRON_TAG not in line]
    existing_tagged = [line for line in lines if CRON_TAG in line]

    if remove:
        if not existing_tagged:
            return existing, False
        return "\n".join(kept) + ("\n" if kept else ""), True

    # install / replace
    if existing_tagged == new_lines:
        return existing, False
    kept.extend(new_lines)
    return "\n".join(kept) + "\n", True


def execute(paths: Paths, *, uninstall: bool) -> int:
    binary = shutil.which("claude-rotate")
    if not binary:
        print("error: claude-rotate is not on PATH", file=sys.stderr)
        return 1

    paths.state_dir.mkdir(parents=True, exist_ok=True)
    new_lines = build_cron_lines(binary, paths.state_dir)

    # Read current crontab — exit 1 when the user has none (no crontab)
    read = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    existing = read.stdout if read.returncode == 0 else ""

    merged, changed = merge_crontab(existing, new_lines, remove=uninstall)
    if not changed:
        print("sync cron entries already up to date — nothing to do", file=sys.stderr)
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
    print(
        f"sync cron entries {action} ({CRON_SCHEDULE_PERIODIC} + {CRON_SCHEDULE_REBOOT})",
        file=sys.stderr,
    )
    return 0
