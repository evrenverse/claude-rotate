"""Codex quota, read live from Codex's own app server.

The TUI's ``/status`` gets its numbers from the app server's
``account/rateLimits/read``; asking the same method over stdio JSON-RPC
returns exactly what ``/status`` shows. It is one backend usage read and
costs no model tokens. (``codex exec "/status"`` is no route — there the
slash command reaches the model as a prompt.)

Session rollouts are deliberately not used, not even as a fallback: they
only carry the limits as of a session's last turn, and a premium-model
session logs a bucket with no windows at all. Read that way, the 5h window
showed 0% while ``/status`` said 100%.
"""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any

from claude_rotate.providers import ProviderQuota, ProviderWindow

# The app server answers in about a second; this only bounds a hung one.
TIMEOUT_SECONDS = 15

_CLIENT_INFO = {"name": "claude-rotate", "version": "0"}
_REQUESTS = (
    {"id": 1, "method": "initialize", "params": {"clientInfo": _CLIENT_INFO}},
    {"method": "initialized"},
    {"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}},
)

# Labelled by duration: a plan with a weekly window only reports it as `primary`.
_BY_MINUTES: dict[object, str] = {300: "5h", 10080: "week"}
_BY_POSITION = {"primary": "5h", "secondary": "week"}


def parse_rate_limits(result: dict[str, Any], *, now: float) -> ProviderQuota:
    """The ``account/rateLimits/read`` result as one quota row."""
    limits = result.get("rateLimits") or {}
    windows = []
    for key, fallback in _BY_POSITION.items():
        window = limits.get(key)
        if not isinstance(window, dict) or window.get("usedPercent") is None:
            continue
        windows.append(
            ProviderWindow(
                label=_BY_MINUTES.get(window.get("windowDurationMins"), fallback),
                used_pct=float(window["usedPercent"]),
                reset_secs=max(0, int((window.get("resetsAt") or now) - now)),
            )
        )
    return ProviderQuota(
        provider="codex",
        account=str(limits.get("planType") or "codex"),
        windows=tuple(windows),
    )


def fetch(now: float) -> list[ProviderQuota]:
    """Ask ``codex app-server``; empty list when Codex is not installed."""
    try:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return []

    # The server shuts down on stdin EOF before it answers, so stdin stays open
    # until the reply arrives; the timer's kill is what ends a hung server.
    timer = threading.Timer(TIMEOUT_SECONDS, proc.kill)
    timer.start()
    reply: dict[str, Any] = {}
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in _REQUESTS))
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                reply = message
                break
    except OSError:
        pass  # server died on start (e.g. a Codex without app-server)
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()

    if "result" in reply:
        return [parse_rate_limits(reply["result"], now=now)]
    error = (reply.get("error") or {}).get("message") or "no answer from codex app-server"
    return [ProviderQuota(provider="codex", account="codex", note=error)]
