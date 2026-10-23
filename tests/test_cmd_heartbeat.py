from __future__ import annotations

import io
from pathlib import Path

import pytest

from claude_rotate import sessions
from claude_rotate.commands.heartbeat import execute
from claude_rotate.config import Paths


def _paths(tmp_path: Path) -> Paths:
    return Paths(config_dir=tmp_path / "c", cache_dir=tmp_path / "ca", state_dir=tmp_path / "s")


# The default walks the real process tree; test records use pid 1.
_own = lambda pid: True  # noqa: E731


def test_heartbeat_active_touches_record(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    sessions.write_record(p, sessions.SessionRecord("u1", "matri", 1, 1.0, 2.0, 2.0))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION", "u1")
    monkeypatch.setattr("time.time", lambda: 555.0)

    assert execute(p, "active", fired_by=_own) == 0
    assert sessions.read_records(p)[0].last_active == 555.0


def test_heartbeat_end_removes_record(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    sessions.write_record(p, sessions.SessionRecord("u1", "matri", 1, 1.0, 2.0, 2.0))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION", "u1")

    assert execute(p, "end", fired_by=_own) == 0
    assert sessions.read_records(p) == []


def test_heartbeat_without_env_is_noop(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    monkeypatch.delenv("CLAUDE_ROTATE_SESSION", raising=False)
    assert execute(p, "active") == 0  # never raises, even with nothing to do


def test_heartbeat_records_session_id_from_hook_input(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    sessions.write_record(p, sessions.SessionRecord("u1", "acct", 1, 1.0, 2.0, 2.0))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION", "u1")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"session_id": "s-9", "hook_event_name": "UserPromptSubmit"}'),
    )
    assert execute(p, "active", fired_by=_own) == 0
    assert sessions.read_records(p)[0].claude_session_id == "s-9"


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"session_id": 5}'])
def test_heartbeat_tolerates_unusable_hook_input(tmp_path, monkeypatch, stdin) -> None:
    p = _paths(tmp_path)
    sessions.write_record(p, sessions.SessionRecord("u1", "acct", 1, 1.0, 2.0, 2.0))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION", "u1")
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    monkeypatch.setattr("time.time", lambda: 7.0)
    assert execute(p, "active", fired_by=_own) == 0
    rec = sessions.read_records(p)[0]
    assert (rec.last_active, rec.claude_session_id) == (7.0, None)


def _own_session(tmp_path, monkeypatch) -> tuple[Paths, sessions.SessionRecord]:
    p = _paths(tmp_path)
    rec = sessions.SessionRecord("u1", "acct", 4242, 1.0, 2.0, 2.0, claude_session_id="s-outer")
    sessions.write_record(p, rec)
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION", "u1")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "s-nested"}'))
    monkeypatch.setattr("time.time", lambda: 555.0)
    return p, rec


def test_heartbeat_acts_for_the_sessions_own_process(tmp_path, monkeypatch) -> None:
    p, _ = _own_session(tmp_path, monkeypatch)
    assert execute(p, "active", fired_by=lambda pid: pid == 4242) == 0
    rec = sessions.read_records(p)[0]
    assert (rec.last_active, rec.claude_session_id) == (555.0, "s-nested")
    assert execute(p, "end", fired_by=lambda pid: pid == 4242) == 0
    assert sessions.read_records(p) == []


@pytest.mark.parametrize("event", ["active", "end"])
def test_heartbeat_ignores_a_nested_claude(tmp_path, monkeypatch, event) -> None:
    p, rec = _own_session(tmp_path, monkeypatch)
    assert execute(p, event, fired_by=lambda pid: False) == 0
    assert sessions.read_records(p) == [rec]


@pytest.mark.parametrize("event", ["active", "end"])
def test_heartbeat_ignores_a_failed_process_lookup(tmp_path, monkeypatch, event) -> None:
    p, rec = _own_session(tmp_path, monkeypatch)

    def broken(pid: int) -> bool:
        raise OSError("no /proc")

    assert execute(p, event, fired_by=broken) == 0
    assert sessions.read_records(p) == [rec]


@pytest.mark.parametrize("event", ["active", "end"])
def test_heartbeat_for_a_missing_record_never_asks_who_fired_it(
    tmp_path, monkeypatch, event
) -> None:
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION", "gone")
    asked: list[int] = []  # execute() swallows exceptions, so record the calls instead of raising

    assert execute(_paths(tmp_path), event, fired_by=lambda pid: asked.append(pid) or True) == 0
    assert asked == []


@pytest.mark.parametrize("reason", ["clear", "resume"])
def test_heartbeat_end_keeps_the_record_of_a_session_that_keeps_running(
    tmp_path, monkeypatch, reason
) -> None:
    p, rec = _own_session(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(f'{{"reason": "{reason}"}}'))
    assert execute(p, "end", fired_by=_own) == 0
    assert sessions.read_records(p) == [rec]


@pytest.mark.parametrize(
    "stdin",
    [f'{{"reason": "{r}"}}' for r in ("logout", "prompt_input_exit", "other")] + ["", "not json"],
)
def test_heartbeat_end_removes_the_record_for_any_other_reason(
    tmp_path, monkeypatch, stdin
) -> None:
    p, _ = _own_session(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert execute(p, "end", fired_by=_own) == 0
    assert sessions.read_records(p) == []
