"""Live-session registry — tracks how many sessions run per account.

Source of truth is one JSON file per session under ``state/sessions/``, keyed
by a run-uuid. Written before ``execvpe`` (the PID survives the exec, so the
record points at the real ``claude`` process) and reaped lazily by checking
whether ``(pid, start_time)`` is still alive. A heartbeat hook refreshes
``last_active``; everything degrades gracefully when the hook is absent.

Pure + testable: process liveness and ``now`` are injectable.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_rotate.config import Paths


@dataclass(frozen=True)
class SessionRecord:
    uuid: str
    account: str
    pid: int
    start_time: float
    started_at: float
    last_active: float

    def to_dict(self) -> dict[str, object]:
        return {
            "uuid": self.uuid,
            "account": self.account,
            "pid": self.pid,
            "start_time": self.start_time,
            "started_at": self.started_at,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> SessionRecord:
        return cls(
            uuid=str(raw["uuid"]),
            account=str(raw["account"]),
            pid=int(raw["pid"]),  # type: ignore[arg-type]
            start_time=float(raw["start_time"]),  # type: ignore[arg-type]
            started_at=float(raw["started_at"]),  # type: ignore[arg-type]
            last_active=float(raw["last_active"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class SessionLoad:
    active: int
    idle: int

    @property
    def open(self) -> int:
        return self.active + self.idle

    def weighted(self, *, idle_weight: float) -> float:
        return self.active + self.idle * idle_weight
