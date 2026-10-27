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


def test_roundtrip_preserves_opus_pct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    r = ProbeResult(
        ok=True,
        http_code=200,
        h5_pct=5.0,
        w7_pct=50.0,
        h5_reset_secs=3600,
        w7_reset_secs=86400,
        w7_opus_pct=72.0,
    )
    cache.save("main", r)

    monkeypatch.setattr(time, "time", lambda: 1_060.0)
    loaded = cache.load("main")
    assert loaded is not None
    assert loaded.w7_opus_pct == 72.0


def test_load_clamps_opus_pct_when_weekly_reset_elapsed(
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
        w7_opus_pct=95.0,
    )
    cache.save("main", r)

    monkeypatch.setattr(time, "time", lambda: 2_000.0)
    loaded = cache.load("main")
    assert loaded is not None
    assert loaded.w7_opus_pct == 0.0


def test_roundtrip_preserves_scoped_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_rotate.selection import ScopedLimit

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    r = ProbeResult(
        ok=True,
        http_code=200,
        h5_pct=5.0,
        w7_pct=50.0,
        h5_reset_secs=3600,
        w7_reset_secs=86400,
        w7_scoped=(ScopedLimit(label="fable", pct=35.0, reset_secs=86400),),
    )
    cache.save("main", r)

    monkeypatch.setattr(time, "time", lambda: 1_060.0)
    loaded = cache.load("main")
    assert loaded is not None
    # Cache-served scoped values are stale by definition and carry their age.
    assert loaded.w7_scoped == (
        ScopedLimit(label="fable", pct=35.0, reset_secs=86340, stale=True, age_secs=60),
    )


def test_load_clamps_scoped_pct_when_its_reset_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_rotate.selection import ScopedLimit

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    r = ProbeResult(
        ok=True,
        http_code=200,
        h5_pct=50.0,
        w7_pct=90.0,
        h5_reset_secs=60,
        w7_reset_secs=7200,
        w7_scoped=(ScopedLimit(label="fable", pct=95.0, reset_secs=120),),
    )
    cache.save("main", r)

    monkeypatch.setattr(time, "time", lambda: 1_500.0)
    loaded = cache.load("main")
    assert loaded is not None
    assert loaded.w7_scoped == (
        ScopedLimit(label="fable", pct=0.0, reset_secs=0, stale=True, age_secs=500),
    )


def _probe(scoped: tuple = (), **kw) -> ProbeResult:
    defaults = dict(
        ok=True, http_code=200, h5_pct=5.0, w7_pct=50.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    defaults.update(kw)
    return ProbeResult(w7_scoped=scoped, **defaults)


def test_save_with_empty_scoped_keeps_last_known_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_rotate.selection import ScopedLimit

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    cache.save("main", _probe(scoped=(ScopedLimit(label="fable", pct=57.0, reset_secs=86400),)))

    # Next probe's OAuth usage fetch failed (429) -> empty scoped tuple.
    monkeypatch.setattr(time, "time", lambda: 1_100.0)
    cache.save("main", _probe())

    loaded = cache.load("main")
    assert loaded is not None
    # The preserved value keeps its ORIGINAL fetch time (age 100s), not the
    # newer probe's — that age is exactly what renderers flag as stale.
    assert loaded.w7_scoped == (
        ScopedLimit(label="fable", pct=57.0, reset_secs=86300, stale=True, age_secs=100),
    )


def test_save_with_empty_scoped_drops_elapsed_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_rotate.selection import ScopedLimit

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    cache.save("main", _probe(scoped=(ScopedLimit(label="fable", pct=57.0, reset_secs=60),)))

    # The scoped window reset in the meantime — the stale value must not survive.
    monkeypatch.setattr(time, "time", lambda: 2_000.0)
    cache.save("main", _probe())
    assert cache.load("main").w7_scoped == ()


def test_save_with_fresh_scoped_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_rotate.selection import ScopedLimit

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    cache.save("main", _probe(scoped=(ScopedLimit(label="fable", pct=57.0, reset_secs=86400),)))
    cache.save("main", _probe(scoped=(ScopedLimit(label="fable", pct=60.0, reset_secs=86400),)))
    assert cache.load("main").w7_scoped[0].pct == 60.0


def test_load_scoped_ignores_max_cache_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_rotate.selection import ScopedLimit

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    cache.save("main", _probe(scoped=(ScopedLimit(label="fable", pct=57.0, reset_secs=86400),)))

    # Way past MAX_CACHE_AGE: load() refuses, load_scoped() still serves the
    # value because it stays valid until its own weekly reset — marked stale,
    # with the time since it was actually fetched.
    monkeypatch.setattr(time, "time", lambda: 1_000.0 + 3600.0)
    assert cache.load("main") is None
    assert cache.load_scoped("main") == (
        ScopedLimit(label="fable", pct=57.0, reset_secs=86400 - 3600, stale=True, age_secs=3600),
    )


def test_load_scoped_drops_elapsed_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_rotate.selection import ScopedLimit

    cache = UsageCache(make_paths(tmp_path))
    assert cache.load_scoped("missing") == ()

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache.save("main", _probe(scoped=(ScopedLimit(label="fable", pct=57.0, reset_secs=60),)))
    monkeypatch.setattr(time, "time", lambda: 2_000.0)
    assert cache.load_scoped("main") == ()


def test_load_scoped_legacy_entry_is_stale_with_unknown_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-``fetched_at`` cache files (3-element entries) load as stale, age unknown."""
    from claude_rotate.selection import ScopedLimit

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    paths = make_paths(tmp_path)
    (paths.usage_dir).mkdir(parents=True)
    (paths.usage_dir / "main.json").write_text(
        json.dumps({"w7_scoped": [["fable", 88.0, 1_000.0 + 86400]]})
    )
    cache = UsageCache(paths)
    assert cache.load_scoped("main") == (
        ScopedLimit(label="fable", pct=88.0, reset_secs=86400, stale=True, age_secs=None),
    )


def test_save_throttles_history_to_one_sample_per_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status --watch`` saves every few seconds; the 500-point cap must outlast 12 h."""
    cache = UsageCache(make_paths(tmp_path))
    history_file = tmp_path / "cache" / "usage" / "main.history.json"

    def stamps(at: float) -> list[float]:
        monkeypatch.setattr(time, "time", lambda: at)
        cache.save("main", _probe())
        return [e[0] for e in json.loads(history_file.read_text())]

    assert stamps(1_000.0) == [1_000.0]
    assert stamps(1_010.0) == [1_000.0]  # 10 s later: skipped
    assert stamps(1_061.0) == [1_000.0, 1_061.0]  # 61 s after the last sample: kept
    probe_file = tmp_path / "cache" / "usage" / "main.json"  # the probe cache is not throttled
    assert json.loads(probe_file.read_text())["probed_at"] == 1_061.0


def test_save_preserves_fetch_age_across_repeated_failed_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original fetch time survives any number of backfill saves."""
    from claude_rotate.selection import ScopedLimit

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    cache.save("main", _probe(scoped=(ScopedLimit(label="fable", pct=57.0, reset_secs=86400),)))

    # Two consecutive probes whose OAuth fetch failed -> scoped preserved.
    monkeypatch.setattr(time, "time", lambda: 5_000.0)
    cache.save("main", _probe())
    monkeypatch.setattr(time, "time", lambda: 9_000.0)
    cache.save("main", _probe())

    monkeypatch.setattr(time, "time", lambda: 10_000.0)
    assert cache.load_scoped("main") == (
        ScopedLimit(label="fable", pct=57.0, reset_secs=86400 - 9_000, stale=True, age_secs=9_000),
    )


def test_usage_cache_probed_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    assert cache.probed_at("main") is None
    cache.save("main", _probe())
    assert cache.probed_at("main") == 1_000.0
    assert cache.probed_at("unknown") is None


def test_load_max_age_none_ignores_the_staleness_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    cache = UsageCache(make_paths(tmp_path))
    cache.save("main", _probe(h5_reset_secs=6 * 3600))

    monkeypatch.setattr(
        time, "time", lambda: 1_000.0 + 2 * 3600
    )  # past the 10 min cap, window open
    assert cache.load("main") is None
    old = cache.load("main", max_age=None)
    assert old is not None
    assert old.h5_pct == 5.0
    assert old.h5_reset_secs == 4 * 3600  # still wound forward to now
