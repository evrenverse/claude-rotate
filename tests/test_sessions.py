from __future__ import annotations

import json
from pathlib import Path

from claude_rotate.config import Paths
from claude_rotate.sessions import SessionLoad, SessionRecord


def test_session_record_roundtrip() -> None:
    rec = SessionRecord(
        uuid="u1",
        account="matri",
        pid=4242,
        start_time=1000.0,
        started_at=2000.0,
        last_active=2050.0,
    )
    assert SessionRecord.from_dict(rec.to_dict()) == rec


def test_session_load_weighted() -> None:
    load = SessionLoad(active=2, idle=3)
    assert load.open == 5
    assert load.weighted(idle_weight=0.3) == 2 + 3 * 0.3


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def test_write_read_remove_record(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    rec = SessionRecord("u1", "matri", 4242, 1000.0, 2000.0, 2000.0)
    sessions.write_record(p, rec)

    got = sessions.read_records(p)
    assert got == [rec]

    sessions.remove_record(p, "u1")
    assert sessions.read_records(p) == []


def test_touch_updates_last_active(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    sessions.write_record(p, SessionRecord("u1", "matri", 1, 1.0, 2.0, 2.0))
    sessions.touch(p, "u1", now=99.0)
    assert sessions.read_records(p)[0].last_active == 99.0


def test_record_round_trips_claude_session_id(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    rec = SessionRecord("u1", "acct", 1, 1.0, 2.0, 2.0, claude_session_id="s-1")
    sessions.write_record(p, rec)
    assert sessions.read_records(p) == [rec]


def test_record_without_claude_session_id_still_reads(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    p.sessions_dir.mkdir(parents=True)
    (p.sessions_dir / "u1.json").write_text(
        json.dumps(
            {
                "uuid": "u1",
                "account": "acct",
                "pid": 1,
                "start_time": 1.0,
                "started_at": 2.0,
                "last_active": 2.0,
            }
        )
    )
    assert sessions.read_records(p)[0].claude_session_id is None


def test_touch_sets_and_replaces_claude_session_id(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    sessions.write_record(p, SessionRecord("u1", "acct", 1, 1.0, 2.0, 2.0))
    sessions.touch(p, "u1", now=3.0, claude_session_id="s-1")
    sessions.touch(p, "u1", now=4.0, claude_session_id="s-2")
    sessions.touch(p, "u1", now=5.0)
    rec = sessions.read_records(p)[0]
    assert (rec.last_active, rec.claude_session_id) == (5.0, "s-2")


def test_read_records_ignores_corrupt_files(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    p.sessions_dir.mkdir(parents=True)
    (p.sessions_dir / "bad.json").write_text("{not json")
    for name, body in (("list", "[]"), ("null", "null"), ("str", '"x"'), ("int", "5")):
        (p.sessions_dir / f"{name}.json").write_text(body)  # valid JSON, not an object
    sessions.write_record(p, SessionRecord("u1", "matri", 1, 1.0, 2.0, 2.0))
    assert [r.uuid for r in sessions.read_records(p)] == ["u1"]


def test_reap_removes_dead_records(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    sessions.write_record(p, SessionRecord("alive", "matri", 1, 1.0, 2.0, 2.0))
    sessions.write_record(p, SessionRecord("dead", "ply", 2, 1.0, 2.0, 2.0))

    def fake_liveness(pid: int, start_time: float) -> bool:
        return pid == 1

    sessions.reap(p, liveness=fake_liveness)
    assert [r.uuid for r in sessions.read_records(p)] == ["alive"]


_LEGACY = {
    "account_name": "a",
    "session_id": "x",
    "cwd": "/",
    "created_at": 1,
    "transcript_path": "/t",
}


def _write_aged(path: Path, body: object, *, age: float, now: float) -> Path:
    import os

    # str and bytes are written raw, anything else as JSON.
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body if isinstance(body, str) else json.dumps(body))
    os.utime(path, (now - age, now - age))
    return path


def test_reap_unreadable_deletes_only_old_unparseable_files(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    p.sessions_dir.mkdir(parents=True)
    now = 1_000_000.0
    day = 86_400.0
    old = _write_aged(p.sessions_dir / "old.json", _LEGACY, age=2 * day, now=now)
    fresh = _write_aged(p.sessions_dir / "fresh.json", _LEGACY, age=3_600.0, now=now)
    garbage = _write_aged(p.sessions_dir / "garbage.json", "{not json", age=2 * day, now=now)
    # Not UTF-8: counts as "not a record", unlike a failed read.
    binary = _write_aged(p.sessions_dir / "binary.json", b"\x80\x81", age=2 * day, now=now)
    # Valid records stay however old they are, with or without claude_session_id.
    valid = SessionRecord("u1", "matri", 1, 1.0, 2.0, 2.0)
    kept = _write_aged(p.sessions_dir / "u1.json", valid.to_dict(), age=2 * day, now=now)
    older = {k: v for k, v in valid.to_dict().items() if k != "claude_session_id"}
    kept_older = _write_aged(p.sessions_dir / "u2.json", older, age=2 * day, now=now)

    assert sessions.reap_unreadable(p, now=now) == 3

    assert not old.exists() and not garbage.exists() and not binary.exists()
    assert fresh.exists() and kept.exists() and kept_older.exists()


def test_reap_unreadable_keeps_a_valid_record_that_failed_to_read(tmp_path, monkeypatch) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    p.sessions_dir.mkdir(parents=True)
    now = 1_000_000.0
    valid = SessionRecord("u1", "matri", 1, 1.0, 2.0, 2.0)
    idle = _write_aged(p.sessions_dir / "u1.json", valid.to_dict(), age=2 * 86_400.0, now=now)
    real_open = Path.open

    def flaky_open(self: Path, *args: object, **kwargs: object):
        if self == idle:  # e.g. EMFILE/EIO after a successful stat
            raise OSError("read failed")
        return real_open(self, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", flaky_open)

    assert sessions.reap_unreadable(p, now=now) == 0
    assert idle.exists()


# Files that json.loads or from_dict choke on with errors beyond the usual decode ones.
_ESCAPING_BODIES = {
    # json.loads gives float("inf") and int(inf) raises OverflowError.
    "overflow": json.dumps(
        {**SessionRecord("x", "a", 1, 1.0, 2.0, 2.0).to_dict(), "pid": "@"}
    ).replace('"@"', "1e999"),
    # json.loads raises RecursionError.
    "nested": "[" * 100_000 + "]" * 100_000,
}


def test_overflowing_and_too_deep_files_are_skipped_not_raised(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    now = 1000.0
    sessions.write_record(p, SessionRecord("u1", "a", 1, 1.0, 2.0, now, claude_session_id="s-1"))
    for name, body in _ESCAPING_BODIES.items():
        _write_aged(p.sessions_dir / f"{name}.json", body, age=60.0, now=now)

    assert [r.uuid for r in sessions.read_records(p)] == ["u1"]
    assert sessions.count_load(p, now=now, active_window=90.0, liveness=lambda *_: True) == {
        "a": SessionLoad(active=1, idle=0)
    }
    assert sessions.claude_sessions_by_account(p, liveness=lambda *_: True) == {"a": ["s-1"]}
    for name, body in _ESCAPING_BODIES.items():
        sessions.touch(p, name, now=now, claude_session_id="s-2")  # no-op, must not raise
        assert (p.sessions_dir / f"{name}.json").read_text() == body


def test_reap_unreadable_deletes_overflowing_and_too_deep_files_once_old(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    p.sessions_dir.mkdir(parents=True)
    now = 1_000_000.0
    old = [
        _write_aged(p.sessions_dir / f"old-{name}.json", body, age=2 * 86_400.0, now=now)
        for name, body in _ESCAPING_BODIES.items()
    ]
    fresh = [
        _write_aged(p.sessions_dir / f"fresh-{name}.json", body, age=3_600.0, now=now)
        for name, body in _ESCAPING_BODIES.items()
    ]

    assert sessions.reap_unreadable(p, now=now) == 2

    assert not any(path.exists() for path in old)
    assert all(path.exists() for path in fresh)


def test_reap_unreadable_without_sessions_dir_is_a_noop(tmp_path) -> None:
    from claude_rotate import sessions

    assert sessions.reap_unreadable(_paths(tmp_path), now=1_000_000.0) == 0


def test_reap_and_count_load_delete_old_unparseable_files(tmp_path) -> None:
    import time

    from claude_rotate import sessions

    p = _paths(tmp_path)
    p.sessions_dir.mkdir(parents=True)
    now = time.time()
    via_reap = _write_aged(p.sessions_dir / "a.json", _LEGACY, age=2 * 86_400.0, now=now)
    sessions.reap(p)
    assert not via_reap.exists()

    via_count_load = _write_aged(p.sessions_dir / "b.json", _LEGACY, age=2 * 86_400.0, now=now)
    assert sessions.count_load(p, now=now, active_window=90.0) == {}
    assert not via_count_load.exists()


def test_is_alive_matches_start_time(tmp_path) -> None:
    import os

    from claude_rotate import sessions

    me = os.getpid()
    st = sessions.process_start_time(me)
    assert st is not None
    assert sessions.is_alive(me, st) is True
    # A mismatched start_time (PID reuse) must read as dead.
    assert sessions.is_alive(me, st + 9999.0) is False
    # A surely-dead PID reads as dead.
    assert sessions.is_alive(2_000_000_000, 0.0) is False


def test_count_load_classifies_active_idle_and_reaps(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    now = 1000.0
    # matri: one active (fresh), one idle (stale); ply: one active; dead: gone
    sessions.write_record(p, SessionRecord("a1", "matri", 1, 0.0, 0.0, now - 10))
    sessions.write_record(p, SessionRecord("a2", "matri", 2, 0.0, 0.0, now - 500))
    sessions.write_record(p, SessionRecord("b1", "ply", 3, 0.0, 0.0, now - 1))
    sessions.write_record(p, SessionRecord("d1", "spend", 9, 0.0, 0.0, now))

    def fake_liveness(pid: int, start_time: float) -> bool:
        return pid != 9  # spend's process is dead

    loads = sessions.count_load(p, now=now, active_window=90.0, liveness=fake_liveness)
    assert loads["matri"] == sessions.SessionLoad(active=1, idle=1)
    assert loads["ply"] == sessions.SessionLoad(active=1, idle=0)
    assert "spend" not in loads
    # dead record was reaped from disk
    assert {r.uuid for r in sessions.read_records(p)} == {"a1", "a2", "b1"}


def test_claude_sessions_by_account_lists_live_ids(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    for uuid, acct, pid, sid in [
        ("u1", "a", 1, "s-2"),
        ("u2", "a", 2, "s-1"),
        ("u3", "b", 3, None),
        ("u4", "c", 4, "s-3"),
        ("u5", "a", 5, "s-1"),
    ]:
        sessions.write_record(
            p, SessionRecord(uuid, acct, pid, 1.0, 2.0, 2.0, claude_session_id=sid)
        )

    def alive(pid: int, start_time: float) -> bool:
        return pid != 4  # c's process is dead

    assert sessions.claude_sessions_by_account(p, liveness=alive) == {"a": ["s-1", "s-2"]}


def test_read_record_returns_one_record_or_none(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    rec = SessionRecord("u1", "acct", 1, 1.0, 2.0, 2.0)
    sessions.write_record(p, rec)
    (p.sessions_dir / "bad.json").write_text("{not json")
    assert sessions.read_record(p, "u1") == rec
    assert sessions.read_record(p, "missing") is None
    assert sessions.read_record(p, "bad") is None


class _Proc:
    def __init__(self, pid: int, name: str = "zsh", exe: str = "/bin/zsh") -> None:
        self.pid, self._name, self._exe = pid, name, exe

    def name(self) -> str:
        return self._name

    def exe(self) -> str:
        return self._exe


_CLAUDE_EXE = "/home/u/.local/share/claude/versions/2.1.0"


def _claude(pid: int) -> _Proc:
    return _Proc(pid, "claude", _CLAUDE_EXE)


def test_nearest_claude_is_the_record_process() -> None:
    from claude_rotate.sessions import _nearest_claude_is

    assert _nearest_claude_is(10, [_Proc(11), _claude(10)], _CLAUDE_EXE)


def test_nearest_claude_is_a_nested_one() -> None:
    from claude_rotate.sessions import _nearest_claude_is

    chain = [_Proc(31), _claude(30), _Proc(11), _claude(10)]  # hook, nested claude, shell, outer
    assert not _nearest_claude_is(10, chain, _CLAUDE_EXE)


def test_nearest_claude_found_by_exe_when_named_node() -> None:
    from claude_rotate.sessions import _nearest_claude_is

    nested = _Proc(30, "node", _CLAUDE_EXE)
    assert not _nearest_claude_is(10, [_Proc(31), nested, _Proc(11), _claude(10)], _CLAUDE_EXE)


def test_nearest_claude_is_false_without_the_record_process() -> None:
    from claude_rotate.sessions import _nearest_claude_is

    assert not _nearest_claude_is(10, [_Proc(11), _Proc(12)], _CLAUDE_EXE)


def test_hook_fired_by_walks_the_real_ancestors(monkeypatch) -> None:
    from claude_rotate import sessions

    class Fake:
        def __init__(self, pid: int | None = None) -> None:
            self.pid = pid

        def parents(self) -> list[_Proc]:
            return [_Proc(11), _claude(10)]

        def exe(self) -> str:
            return _CLAUDE_EXE

    monkeypatch.setattr("psutil.Process", Fake)
    assert sessions.hook_fired_by(10)
    assert not sessions.hook_fired_by(99)


def test_hook_fired_by_is_false_when_the_process_lookup_fails(monkeypatch) -> None:
    import psutil

    from claude_rotate import sessions

    def boom(pid: int | None = None) -> None:
        raise psutil.NoSuchProcess(1)

    monkeypatch.setattr("psutil.Process", boom)
    assert not sessions.hook_fired_by(10)


def test_file_lock_acquires_and_releases(tmp_path) -> None:
    from claude_rotate.config import file_lock

    p = _paths(tmp_path)
    with file_lock(p.sessions_lock):
        pass  # acquire + release without error
    # second acquisition after release also works
    with file_lock(p.sessions_lock):
        assert p.sessions_lock.exists()


def test_file_lock_times_out_while_another_holder_has_it(tmp_path) -> None:
    """The wait ceiling must surface as LockTimeoutError, not hang forever."""
    import pytest

    from claude_rotate.config import file_lock
    from claude_rotate.errors import LockTimeoutError

    p = _paths(tmp_path)

    def take_it_again() -> None:
        # A second fd for the same file cannot take the exclusive lock.
        with file_lock(p.sessions_lock, timeout=0):
            pass

    with file_lock(p.sessions_lock), pytest.raises(LockTimeoutError):
        take_it_again()
