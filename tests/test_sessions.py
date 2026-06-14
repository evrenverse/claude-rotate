from __future__ import annotations

from pathlib import Path

from claude_rotate.config import Paths
from claude_rotate.sessions import SessionLoad, SessionRecord


def test_session_record_roundtrip() -> None:
    rec = SessionRecord(
        uuid="u1", account="matri", pid=4242,
        start_time=1000.0, started_at=2000.0, last_active=2050.0,
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


def test_read_records_ignores_corrupt_files(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    p.sessions_dir.mkdir(parents=True)
    (p.sessions_dir / "bad.json").write_text("{not json")
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

    loads = sessions.count_load(
        p, now=now, active_window=90.0, liveness=fake_liveness
    )
    assert loads["matri"] == sessions.SessionLoad(active=1, idle=1)
    assert loads["ply"] == sessions.SessionLoad(active=1, idle=0)
    assert "spend" not in loads
    # dead record was reaped from disk
    assert {r.uuid for r in sessions.read_records(p)} == {"a1", "a2", "b1"}


def test_file_lock_acquires_and_releases(tmp_path) -> None:
    from claude_rotate import sessions

    p = _paths(tmp_path)
    with sessions.file_lock(p.sessions_lock):
        pass  # acquire + release without error
    # second acquisition after release also works
    with sessions.file_lock(p.sessions_lock):
        assert p.sessions_lock.exists()
