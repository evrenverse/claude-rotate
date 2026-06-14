"""`claude-rotate __heartbeat <event>` — internal, called by the Claude Code hook.

``active`` (UserPromptSubmit) refreshes the session's last_active;
``end`` (SessionEnd) removes the record. Reads CLAUDE_ROTATE_SESSION from the
environment (injected by exec_claude). Must NEVER fail — a broken hook must not
disrupt the claude session — so it always returns 0 and swallows everything.
"""

from __future__ import annotations

import os
import time

from claude_rotate import sessions
from claude_rotate.config import Paths


def execute(paths: Paths, event: str) -> int:
    try:
        uuid = os.environ.get("CLAUDE_ROTATE_SESSION")
        if not uuid:
            return 0
        if event == "end":
            sessions.remove_record(paths, uuid)
        else:  # "active" and any unknown event are treated as a heartbeat
            sessions.touch(paths, uuid, now=time.time())
    except Exception:
        return 0
    return 0
