"""`claude-rotate sync-credentials` — cron entry point.

Two jobs, run on every cron tick:

1. **reconcile** — copy any in-session token rotation from
   ~/.claude/.credentials.json back into accounts.json.
2. **proactive refresh** — for every OAuth-path account whose access
   token is older than the refresh threshold, exchange the refresh
   token for a fresh pair. Without this step tokens die silently
   while the PC idles, and the user hits a login prompt the next
   morning.

Designed for cron (2-minute cadence). Always exits 0 to avoid cron
email spam when nothing needed to happen. Prints one-line summaries
to stdout when a change was applied so ``tail -f`` of the log file
is useful.
"""

from __future__ import annotations

from datetime import UTC, datetime

from claude_rotate.config import Paths
from claude_rotate.settings import load_config
from claude_rotate.sync import (
    mirror_session_to_global,
    reconcile_all,
    reconcile_isolated,
    refresh_stale_tokens,
)


def execute(paths: Paths) -> int:
    now = datetime.now(UTC)

    if load_config(paths).session_isolation:
        synced_names = reconcile_isolated(paths, now=now)
        refreshed = refresh_stale_tokens(paths, now=now, isolated=True)
        # Keep the default ~/.claude/.credentials.json fresh so headless consumers
        # (CI, the enniflow worker) that never set CLAUDE_CONFIG_DIR still boot with
        # a live token instead of the frozen, expired isolation-mode leftover.
        mirrored = mirror_session_to_global(paths, now=now)
        if synced_names or refreshed or mirrored:
            stamp = datetime.now(UTC).isoformat(timespec="seconds")
            if synced_names:
                names = ", ".join(synced_names)
                print(f"[{stamp}] credentials synced ({len(synced_names)}): {names}")
            if refreshed:
                names = ", ".join(refreshed)
                print(f"[{stamp}] refreshed {len(refreshed)} stale account(s): {names}")
            if mirrored:
                print(f"[{stamp}] mirrored global fallback credentials: {mirrored}")
        return 0

    synced = reconcile_all(paths, now=now)
    refreshed = refresh_stale_tokens(paths, now=now)

    if synced or refreshed:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        if synced:
            print(f"[{stamp}] credentials synced")
        if refreshed:
            print(f"[{stamp}] refreshed {len(refreshed)} stale account(s): {', '.join(refreshed)}")
    return 0
