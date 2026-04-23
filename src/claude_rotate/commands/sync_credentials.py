"""`claude-rotate sync-credentials` — reconcile .credentials.json → accounts.json.

Designed for cron (2-minute cadence). Always exits 0 to avoid cron email
spam when nothing needs syncing. Prints a one-line summary to stdout
when a change was applied so ``tail -f`` of the log file is useful.
"""

from __future__ import annotations

from datetime import UTC, datetime

from claude_rotate.config import Paths
from claude_rotate.sync import reconcile_all


def execute(paths: Paths) -> int:
    changed = reconcile_all(paths, now=datetime.now(UTC))
    if changed:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        print(f"[{stamp}] credentials synced")
    return 0
