from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_rotate.config import Paths
from claude_rotate.probe import ProbeResult
from claude_rotate.usage_cache import UsageCache


def make_paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def test_save_and_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    r = ProbeResult(
        ok=True,
        http_code=200,
        h5_pct=5.0,
        w7_pct=50.0,
        h5_reset_secs=3600,
        w7_reset_secs=86400,
    )
    cache.save("main", r)

    monkeypatch.setattr(time, "time", lambda: 1_060.0)
    loaded = cache.load("main")
    assert loaded is not None
    assert loaded.ok
    assert loaded.h5_pct == 5.0
    assert loaded.w7_pct == 50.0
    assert loaded.h5_reset_secs == 3540
    assert loaded.w7_reset_secs == 86340


def test_load_missing_returns_none(tmp_path: Path) -> None:
    cache = UsageCache(make_paths(tmp_path))
    assert cache.load("missing") is None


def test_load_clamps_pct_to_zero_when_reset_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    r = ProbeResult(
        ok=True,
        http_code=200,
        h5_pct=50.0,
        w7_pct=90.0,
        h5_reset_secs=60,
        w7_reset_secs=120,
    )
    cache.save("main", r)

    monkeypatch.setattr(time, "time", lambda: 2_000.0)
    loaded = cache.load("main")
    assert loaded is not None
    assert loaded.h5_pct == 0.0
    assert loaded.w7_pct == 0.0
    assert loaded.h5_reset_secs == 0
    assert loaded.w7_reset_secs == 0


def test_load_too_old_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache older than MAX_CACHE_AGE is considered stale."""
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    r = ProbeResult(
        ok=True,
        http_code=200,
        h5_pct=10.0,
        w7_pct=10.0,
        h5_reset_secs=3600,
        w7_reset_secs=86400,
    )
    cache.save("main", r)

    monkeypatch.setattr(time, "time", lambda: 1_000.0 + 15 * 60)
    assert cache.load("main") is None


def test_save_writes_file_with_expected_shape(tmp_path: Path) -> None:
    cache = UsageCache(make_paths(tmp_path))
    cache.save(
        "main",
        ProbeResult(
            ok=True, http_code=200, h5_pct=3.0, w7_pct=4.0, h5_reset_secs=10, w7_reset_secs=20
        ),
    )
    data = json.loads((tmp_path / "cache" / "usage" / "main.json").read_text())
    assert set(data.keys()) >= {
        "probed_at",
        "h5_pct",
        "w7_pct",
        "h5_reset_at",
        "w7_reset_at",
        "http_code",
    }
