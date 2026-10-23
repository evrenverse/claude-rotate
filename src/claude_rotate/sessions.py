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
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from claude_rotate.config import Paths, atomic_write_json


@dataclass(frozen=True)
class SessionRecord:
    uuid: str
    account: str
    pid: int
    start_time: float
    started_at: float
    last_active: float
    claude_session_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "uuid": self.uuid,
            "account": self.account,
            "pid": self.pid,
            "start_time": self.start_time,
            "started_at": self.started_at,
            "last_active": self.last_active,
            "claude_session_id": self.claude_session_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> SessionRecord:
        return cls(
            uuid=str(raw["uuid"]),
            account=str(raw["account"]),
            pid=int(raw["pid"]),  # type: ignore[call-overload]
            start_time=float(raw["start_time"]),  # type: ignore[arg-type]
            started_at=float(raw["started_at"]),  # type: ignore[arg-type]
            last_active=float(raw["last_active"]),  # type: ignore[arg-type]
            # Only add optional keys: versions running side by side reap records they cannot parse.
            # Absent in records from older versions. Keep this after the indexed reads above:
            # non-object JSON must fail there with TypeError (which callers catch), not on .get.
            claude_session_id=sid if isinstance(sid := raw.get("claude_session_id"), str) else None,
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
        atomic_write_json(
            _record_path(paths, record.uuid), record.to_dict(), mode=0o644, indent=None
        )
    except OSError:
        return


# Raised for content that is not a session record. ValueError also covers the
# UnicodeDecodeError of non-UTF-8 bytes. An OSError is a failed read, not a verdict.
_NOT_A_RECORD = (json.JSONDecodeError, KeyError, ValueError, TypeError)


def _record_from(data: bytes) -> SessionRecord:
    """The record in ``data``; raises one of ``_NOT_A_RECORD`` if it is not one."""
    return SessionRecord.from_dict(json.loads(data))


def _parse(path: Path) -> SessionRecord | None:
    """The record in ``path``, or None if the file is unreadable or not a record."""
    try:
        return _record_from(path.read_bytes())
    except (OSError, *_NOT_A_RECORD):
        return None


def read_records(paths: Paths) -> list[SessionRecord]:
    """Read all session records. Corrupt/partial files are skipped."""
    out: list[SessionRecord] = []
    if not paths.sessions_dir.is_dir():
        return out
    for path in sorted(paths.sessions_dir.glob("*.json")):
        if (rec := _parse(path)) is not None:
            out.append(rec)
    return out


def reap_unreadable(paths: Paths, *, now: float, min_age: float = 86_400) -> int:
    """Delete session files that no longer parse as a record (e.g. left behind by an
    older format) once they are older than ``min_age`` seconds, so a fresh partial
    write is never touched. Best-effort; returns how many files were deleted."""
    removed = 0
    if not paths.sessions_dir.is_dir():
        return removed
    for path in paths.sessions_dir.glob("*.json"):
        try:
            if now - path.stat().st_mtime <= min_age:
                continue
            data = path.read_bytes()  # a failed read must never count as "not a record"
            try:
                _record_from(data)
            except _NOT_A_RECORD:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def remove_record(paths: Paths, uuid: str) -> None:
    """Delete one record. Best-effort; missing file is fine."""
    try:
        _record_path(paths, uuid).unlink(missing_ok=True)
    except OSError:
        return


def touch(paths: Paths, uuid: str, *, now: float, claude_session_id: str | None = None) -> None:
    """Refresh last_active on an existing record, and replace the Claude session
    ID when one is given. No-op if the record is gone."""
    rec = _parse(_record_path(paths, uuid))
    if rec is None:
        return
    from dataclasses import replace

    write_record(
        paths,
        replace(rec, last_active=now, claude_session_id=claude_session_id or rec.claude_session_id),
    )


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
    """Delete records whose backing process is no longer alive, and old unparseable files."""
    reap_unreadable(paths, now=time.time())
    for rec in read_records(paths):
        if not liveness(rec.pid, rec.start_time):
            remove_record(paths, rec.uuid)


def count_load(
    paths: Paths,
    *,
    now: float,
    active_window: float,
    liveness: Liveness = is_alive,
) -> dict[str, SessionLoad]:
    """Per-account live-session load. Reaps dead records and old unparseable files
    as a side effect.

    A record is *active* when its ``last_active`` is younger than
    ``active_window`` (freshly launched sessions qualify because last_active is
    initialised to started_at), otherwise *idle*. Accounts with no live session
    are absent from the result.
    """
    reap_unreadable(paths, now=now)
    active: dict[str, int] = {}
    idle: dict[str, int] = {}
    for rec in read_records(paths):
        if not liveness(rec.pid, rec.start_time):
            remove_record(paths, rec.uuid)
            continue
        bucket = active if (now - rec.last_active) < active_window else idle
        bucket[rec.account] = bucket.get(rec.account, 0) + 1
    names = set(active) | set(idle)
    return {name: SessionLoad(active=active.get(name, 0), idle=idle.get(name, 0)) for name in names}


def claude_sessions_by_account(
    paths: Paths, *, liveness: Liveness = is_alive
) -> dict[str, list[str]]:
    """Claude Code session IDs of the live sessions, per account (sorted, unique).

    Read-only: dead records are left for ``count_load``/``reap``. Accounts
    without a recorded ID are absent from the result.
    """
    ids: dict[str, set[str]] = {}
    for rec in read_records(paths):
        if rec.claude_session_id and liveness(rec.pid, rec.start_time):
            ids.setdefault(rec.account, set()).add(rec.claude_session_id)
    return {name: sorted(found) for name, found in ids.items()}
