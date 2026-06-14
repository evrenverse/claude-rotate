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
