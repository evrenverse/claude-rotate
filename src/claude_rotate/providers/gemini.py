"""Gemini quota, read from Antigravity's own `/usage` slash command.

The obvious route is closed: this account's Code Assist tier reports
``UNSUPPORTED_CLIENT`` ("migrate to the Antigravity suite"), so
``cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota`` answers 403
``SUBSCRIPTION_REQUIRED``. Antigravity serves the numbers over its own
protocol instead, and ``agy -p /usage`` is the supported way to ask for
them — it resolves the slash command locally, so it costs no quota.

Output is tab-separated, one row per family and window, and reports what
is *remaining*, which this module inverts into consumed percent:

    Gemini Models\tFive Hour Limit Remaining\t40%\t2026-09-19T11:23:00Z
"""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime

from claude_rotate.providers import ProviderQuota, ProviderWindow

# agy needs a few seconds to start its language server before it answers.
TIMEOUT_SECONDS = 25

_LABELS = {"five hour": "5h", "weekly": "week"}
_ORDER = ("5h", "week")


def _label(text: str) -> str | None:
    lowered = text.lower()
    return next((v for k, v in _LABELS.items() if lowered.startswith(k)), None)


def parse_usage(text: str, *, now: float) -> list[ProviderQuota]:
    """Tab-separated `/usage` rows into one quota per model family.

    Families keep the order agy printed them in; windows are re-ordered to
    5h-then-week so the table reads like the Anthropic dashboard.
    """
    families: dict[str, list[ProviderWindow]] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        family, window, remaining, resets_at = (f.strip() for f in fields)
        label = _label(window)
        if label is None or not remaining.endswith("%"):
            continue
        try:
            used = 100.0 - float(remaining[:-1])
            reset = datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        families.setdefault(family, []).append(
            ProviderWindow(label=label, used_pct=used, reset_secs=max(0, int(reset - now)))
        )

    return [
        ProviderQuota(
            provider="gemini",
            account=family,
            windows=tuple(sorted(windows, key=lambda w: _ORDER.index(w.label))),
        )
        for family, windows in families.items()
    ]


def fetch(now: float) -> list[ProviderQuota]:
    """Run ``agy -p /usage``; empty list when Antigravity is not installed or logged out."""
    try:
        proc = subprocess.Popen(
            ["agy", "-p", "/usage", "--print-timeout", f"{TIMEOUT_SECONDS - 5}s"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        return []

    timer = threading.Timer(TIMEOUT_SECONDS, proc.kill)
    timer.start()
    output = ""
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            # Logged out, agy prints a login URL and then waits a minute for the
            # OAuth callback: no subscription to show, so stop it right there.
            if line.startswith("Authentication required"):
                return []
            output += line
    finally:
        timed_out = not timer.is_alive()
        timer.cancel()
        proc.kill()
        proc.wait()

    quotas = parse_usage(output, now=now)
    if quotas:
        return quotas
    # Installed and logged in but unreadable: a hung agy, or an output format
    # that moved on. Surface it rather than hiding the provider entirely.
    note = "agy timed out" if timed_out else "no usage data"
    return [ProviderQuota(provider="gemini", account="gemini", note=note)]
