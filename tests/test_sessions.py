from __future__ import annotations

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
