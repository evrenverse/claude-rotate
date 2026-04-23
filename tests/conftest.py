"""Shared pytest fixtures for claude-rotate tests.

Isolates every test with its own CLAUDE_ROTATE_DIR so nothing can touch
the real user config. Also provides a freezable clock for time-dependent
logic (expiry math, cache extrapolation).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def rotate_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Disposable CLAUDE_ROTATE_DIR for a single test."""
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def frozen_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Returns a mutable [now] list; tests can set `now[0] = new_value`."""
    now = [1_776_854_321.0]  # 2026-04-22T08:00:00Z — a stable reference

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            from datetime import UTC, datetime

            return datetime.fromtimestamp(now[0], tz=tz or UTC)

    monkeypatch.setattr("time.time", lambda: now[0])
    yield now
