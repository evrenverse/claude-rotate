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
from claude_rotate.sync import reconcile_all, refresh_stale_accounts


def execute(paths: Paths) -> int:
    now = datetime.now(UTC)
    synced = reconcile_all(paths, now=now)
    refreshed = refresh_stale_accounts(paths, now=now)

    if synced or refreshed:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        if synced:
            print(f"[{stamp}] credentials synced")
        if refreshed:
            print(f"[{stamp}] refreshed {len(refreshed)} stale account(s): {', '.join(refreshed)}")
    return 0
