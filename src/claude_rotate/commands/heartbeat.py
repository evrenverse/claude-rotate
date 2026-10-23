"""`claude-rotate __heartbeat <event>` — internal, called by the Claude Code hook.

``active`` (UserPromptSubmit) refreshes the session's last_active and records
Claude Code's ``session_id`` from the hook JSON on stdin;
``end`` (SessionEnd) removes the record, except when the hook input's ``reason`` is
``clear`` or ``resume``: the same Claude Code process keeps running then. Acts only
when the hook was fired by the session's own Claude Code process, not by a nested
``claude -p``. Reads CLAUDE_ROTATE_SESSION from the environment (injected by
exec_claude). Must NEVER fail — a broken hook must not disrupt the claude session —
so it always returns 0 and swallows everything.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import TextIO

from claude_rotate import sessions
from claude_rotate.config import Paths

# UserPromptSubmit input carries the full prompt. A payload over 1 MiB is truncated, fails to
# parse, and the previous session ID is kept.
_MAX_HOOK_INPUT = 1 << 20


def _read_hook_input(stream: TextIO) -> dict[str, object]:
    """The hook JSON object on ``stream``; ``{}`` if it is missing, unreadable or not an object."""
    try:
        if stream.isatty():
            return {}
        data = json.loads(stream.read(_MAX_HOOK_INPUT))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_hook_session_id(stream: TextIO) -> str | None:
    """Claude Code's ``session_id`` from the hook JSON on ``stream``; None if unusable."""
    session_id = _read_hook_input(stream).get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def execute(
    paths: Paths, event: str, *, fired_by: sessions.HookOwner = sessions.hook_fired_by
) -> int:
    try:
        uuid = os.environ.get("CLAUDE_ROTATE_SESSION")
        if not uuid:
            return 0
        # A nested `claude -p` inherits CLAUDE_ROTATE_SESSION; its hooks must not touch our record.
        rec = sessions.read_record(paths, uuid)
        if rec is None or not fired_by(rec.pid):
            return 0
        if event == "end":
            # /clear and /resume end the hook's session but not the process: keep its record.
            if _read_hook_input(sys.stdin).get("reason") not in ("clear", "resume"):
                sessions.remove_record(paths, uuid)
        else:  # "active" and any unknown event are treated as a heartbeat
            sessions.touch(
                paths,
                uuid,
                now=time.time(),
                claude_session_id=read_hook_session_id(sys.stdin),
            )
    except Exception:
        return 0
    return 0
