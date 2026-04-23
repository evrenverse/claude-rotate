"""Append-only JSONL log of significant events (probe / exec / login / error).

Never raises — logging failures are swallowed. If the state dir can't be
written to, the call silently becomes a no-op. Everything written here
is diagnostic; the tool must work without it.
"""

from __future__ import annotations

import json
import time
from typing import Any

from claude_rotate.config import Paths


class StateLog:
    def __init__(self, paths: Paths) -> None:
        self._paths = paths

    def event(self, event: str, **fields: Any) -> None:
        payload = {"ts": int(time.time()), "event": event, **fields}
        try:
            self._paths.state_dir.mkdir(parents=True, exist_ok=True)
            with self._paths.log_file.open("a") as f:
                f.write(json.dumps(payload, default=str) + "\n")
        except OSError:
            # Diagnostic log must never break the tool.
            return
