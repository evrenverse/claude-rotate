"""Codex quota, read from the newest session rollout.

Codex has no offline quota command — ``codex exec "/status"`` sends the
slash command to the model as a prompt, burns tokens and answers that
usage is unavailable. It does, however, log a ``rate_limits`` block on
every ``token_count`` event, so the newest rollout carries the last known
numbers for free.

Those numbers are as fresh as the last Codex session, which is why a
window whose reset has already elapsed reads as 0% (the window rolled
over since) and anything older than a few minutes is labelled in ``note``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from claude_rotate.insights import rel_duration
from claude_rotate.providers import ProviderQuota, ProviderWindow

SESSIONS_DIR = Path.home() / ".codex" / "sessions"

# Below this, the measurement passes as current and carries no age note.
_FRESH_SECONDS = 300

_WINDOWS = (("primary", "5h"), ("secondary", "week"))


def _window(bucket: object, label: str, now: float) -> ProviderWindow | None:
    if not isinstance(bucket, dict) or bucket.get("used_percent") is None:
        return None
    reset_secs = max(0, int(float(bucket.get("resets_at") or 0) - now))
    # An elapsed reset means the window rolled over after the rollout was
    # written — the logged usage belongs to a window that no longer exists.
    return ProviderWindow(
        label=label,
        used_pct=0.0 if reset_secs == 0 else float(bucket["used_percent"]),
        reset_secs=reset_secs,
    )


def parse_rollout(lines: Iterable[str], *, now: float) -> ProviderQuota | None:
    """Last ``rate_limits`` block in a rollout, or None if it logged none.

    Tolerates a truncated final line: a running session's rollout is read
    while it is still being written to.
    """
    latest: dict[str, object] | None = None
    measured_at: str | None = None
    for line in lines:
        if '"rate_limits"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except (json.JSONDecodeError, AttributeError):
            continue
        limits = payload.get("rate_limits")
        if isinstance(limits, dict):
            latest = limits
            measured_at = json.loads(line).get("timestamp")

    if latest is None:
        return None

    windows = tuple(w for key, label in _WINDOWS if (w := _window(latest.get(key), label, now)))
    return ProviderQuota(
        provider="codex",
        account=str(latest.get("plan_type") or "codex"),
        windows=windows,
        note=_age_note(measured_at, now),
    )


def _age_note(measured_at: str | None, now: float) -> str:
    if not measured_at:
        return ""
    try:
        age = int(now - datetime.fromisoformat(measured_at.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return ""
    if age < _FRESH_SECONDS:
        return ""
    return f"measured {rel_duration(age).strip('()')} ago"


def fetch(now: float) -> list[ProviderQuota]:
    """Newest rollout that carries rate limits; empty when Codex is unused."""
    if not SESSIONS_DIR.is_dir():
        return []
    rollouts = sorted(SESSIONS_DIR.glob("**/rollout-*.jsonl"), key=os.path.getmtime, reverse=True)
    # Only the newest few: a session that ran without hitting the API logs no
    # rate limits at all, so we fall back a couple of files before giving up.
    for path in rollouts[:5]:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                quota = parse_rollout(fh, now=now)
        except OSError:
            continue
        if quota is not None:
            return [quota]
    return []
