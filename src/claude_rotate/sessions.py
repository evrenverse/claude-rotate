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

import psutil

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


def _record_path(paths: Paths, uuid: str) -> Path:
    return paths.sessions_dir / f"{uuid}.json"


def write_record(paths: Paths, record: SessionRecord) -> None:
    """Atomically write one session record. Best-effort; never raises."""
    try:
        paths.sessions_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(paths.sessions_dir), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record.to_dict(), f)
            os.replace(tmp, str(_record_path(paths, record.uuid)))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        return


def read_records(paths: Paths) -> list[SessionRecord]:
    """Read all session records. Corrupt/partial files are skipped."""
    out: list[SessionRecord] = []
    if not paths.sessions_dir.is_dir():
        return out
    for path in sorted(paths.sessions_dir.glob("*.json")):
        try:
            out.append(SessionRecord.from_dict(json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return out


def remove_record(paths: Paths, uuid: str) -> None:
    """Delete one record. Best-effort; missing file is fine."""
    try:
        _record_path(paths, uuid).unlink(missing_ok=True)
    except OSError:
        return


def touch(paths: Paths, uuid: str, *, now: float) -> None:
    """Refresh last_active on an existing record. No-op if it is gone."""
    path = _record_path(paths, uuid)
    try:
        rec = SessionRecord.from_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return
    from dataclasses import replace

    write_record(paths, replace(rec, last_active=now))


# A liveness predicate: (pid, start_time) -> still the same live process?
Liveness = Callable[[int, float], bool]

# create_time() is float seconds; we store it rounded, so allow slack.
_START_TIME_TOLERANCE = 1.5


def process_start_time(pid: int) -> float | None:
    """Process creation time (epoch secs), or None if the pid is gone."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return None


def is_alive(pid: int, start_time: float) -> bool:
    """True iff ``pid`` exists AND its start time matches (guards PID reuse)."""
    actual = process_start_time(pid)
    if actual is None:
        return False
    return abs(actual - start_time) <= _START_TIME_TOLERANCE


def reap(paths: Paths, *, liveness: Liveness = is_alive) -> None:
    """Delete records whose backing process is no longer alive."""
    for rec in read_records(paths):
        if not liveness(rec.pid, rec.start_time):
            remove_record(paths, rec.uuid)
