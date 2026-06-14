from __future__ import annotations

from pathlib import Path

from claude_rotate import sessions
from claude_rotate.commands.heartbeat import execute
from claude_rotate.config import Paths


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "c", cache_dir=tmp_path / "ca", state_dir=tmp_path / "s"
    )


def test_heartbeat_active_touches_record(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    sessions.write_record(p, sessions.SessionRecord("u1", "matri", 1, 1.0, 2.0, 2.0))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION", "u1")
    monkeypatch.setattr("time.time", lambda: 555.0)

    assert execute(p, "active") == 0
    assert sessions.read_records(p)[0].last_active == 555.0


def test_heartbeat_end_removes_record(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    sessions.write_record(p, sessions.SessionRecord("u1", "matri", 1, 1.0, 2.0, 2.0))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION", "u1")

    assert execute(p, "end") == 0
    assert sessions.read_records(p) == []


def test_heartbeat_without_env_is_noop(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    monkeypatch.delenv("CLAUDE_ROTATE_SESSION", raising=False)
    assert execute(p, "active") == 0  # never raises, even with nothing to do
