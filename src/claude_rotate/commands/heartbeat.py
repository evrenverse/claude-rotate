"""`claude-rotate __heartbeat <event>` — internal, called by the Claude Code hook.

``active`` (UserPromptSubmit) refreshes the session's last_active and records
Claude Code's ``session_id`` from the hook JSON on stdin;
``end`` (SessionEnd) removes the record. Reads CLAUDE_ROTATE_SESSION from the
environment (injected by exec_claude). Must NEVER fail — a broken hook must not
disrupt the claude session — so it always returns 0 and swallows everything.
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


def read_hook_session_id(stream: TextIO) -> str | None:
    """Claude Code's ``session_id`` from the hook JSON on ``stream``; None if unusable."""
    try:
        if stream.isatty():
            return None
        session_id = json.loads(stream.read(_MAX_HOOK_INPUT))["session_id"]
    except Exception:
        return None
    return session_id if isinstance(session_id, str) and session_id else None


def execute(paths: Paths, event: str) -> int:
    try:
        uuid = os.environ.get("CLAUDE_ROTATE_SESSION")
        if not uuid:
            return 0
        if event == "end":
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
