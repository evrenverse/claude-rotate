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


@pytest.fixture(autouse=True)
def no_external_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite off this machine's real Codex and Antigravity installs.

    ``status`` reads third-party quota on every run, which means shelling out
    to ``agy`` and scanning ``~/.codex/sessions``. Unguarded, that turns every
    status test into a seconds-long call whose result depends on whoever runs
    the suite. Tests that want provider rows patch ``collect_providers`` (or
    ``_READERS``) themselves.
    """
    monkeypatch.setattr("claude_rotate.providers._READERS", ())
