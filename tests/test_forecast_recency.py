"""Recency-aware forecast: tail-rate from usage history + blended projection.

The plain forecast assumes the window-average burn continues; these cover the
recency blend that weights a recent tail rate, so a late burst pushes the
projection up and a recent calm pulls it down — without breaking the
average-pace fallback (covered in test_dashboard.py).
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_rotate.config import (
    FORECAST_WINDOW_5H_SECONDS,
    USAGE_HISTORY_RETENTION_SECONDS,
    Paths,
)
from claude_rotate.insights import compute_forecast, compute_limit_eta
from claude_rotate.usage_cache import UsageCache


def _cache(tmp_path: Path) -> UsageCache:
    paths = Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )
    paths.usage_dir.mkdir(parents=True, exist_ok=True)
    return UsageCache(paths)


def _write_history(cache: UsageCache, name: str, samples: list[list[float | None]]) -> None:
    cache._history_path(name).write_text(json.dumps(samples))


# --- recent_rate ----------------------------------------------------------


def test_recent_rate_uses_oldest_sample_in_tail_window(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    now = 10_000.0
    # 10min ago pct=20, 30min ago pct=14 — both inside a 1h tail, >5min old.
    _write_history(cache, "acc", [[now - 600, 20.0, 5.0], [now - 1800, 14.0, 4.0]])
    rate = cache.recent_rate("acc", 24.0, window="5h", now=now, tail_secs=3600, min_span=300)
    assert rate is not None
    # Anchored on the oldest in-window sample (30min ago, pct=14): (24-14)/1800.
    assert abs(rate - (10.0 / 1800)) < 1e-9


def test_recent_rate_ignores_samples_younger_than_min_span(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    now = 10_000.0
    # Only a 100s-old sample exists — younger than the 300s noise guard.
    _write_history(cache, "acc", [[now - 100, 20.0, 5.0]])
    assert (
        cache.recent_rate("acc", 24.0, window="5h", now=now, tail_secs=3600, min_span=300) is None
    )


def test_recent_rate_none_when_usage_dropped_reset(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    now = 10_000.0
    _write_history(cache, "acc", [[now - 600, 80.0, 40.0]])
    # pct_now below the past sample → a reset fell inside the span → unreliable.
    assert cache.recent_rate("acc", 5.0, window="5h", now=now, tail_secs=3600, min_span=300) is None


def test_recent_rate_none_without_history(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    assert (
        cache.recent_rate("acc", 24.0, window="5h", now=1.0, tail_secs=3600, min_span=300) is None
    )


def test_recent_rate_none_when_pct_now_unknown(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    _write_history(cache, "acc", [[0.0, 20.0, 5.0]])
    assert (
        cache.recent_rate("acc", None, window="5h", now=10_000.0, tail_secs=3600, min_span=300)
        is None
    )


def test_recent_rate_picks_w7_column(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    now = 100_000.0
    _write_history(cache, "acc", [[now - 7200, 50.0, 10.0]])
    rate = cache.recent_rate("acc", 16.0, window="7d", now=now, tail_secs=43_200, min_span=300)
    assert rate is not None
    assert abs(rate - (6.0 / 7200)) < 1e-9  # uses w7 column: (16-10)/7200


# --- history pruning ------------------------------------------------------


def test_append_history_prunes_beyond_retention(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    now = 1_000_000.0
    stale = now - USAGE_HISTORY_RETENTION_SECONDS - 100
    _write_history(cache, "acc", [[stale, 10.0, 2.0], [now - 100, 20.0, 4.0]])
    cache._append_history("acc", now, 22.0, 5.0)
    kept = json.loads(cache._history_path("acc").read_text())
    timestamps = [e[0] for e in kept]
    assert stale not in timestamps  # pruned
    assert now in timestamps and (now - 100) in timestamps  # recent kept


# --- blended projection ---------------------------------------------------


def _avg(pct: float) -> int | None:
    # 1h into a 5h window: elapsed=3600, horizon=reset=14400.
    return compute_forecast(pct, 14_400, FORECAST_WINDOW_5H_SECONDS)


def test_hot_tail_raises_forecast_above_average() -> None:
    pct, reset = 10.0, 14_400
    hot = 0.01  # 36%/h recent burn, far above the 10%/h window average
    blended = compute_forecast(pct, reset, FORECAST_WINDOW_5H_SECONDS, None, hot)
    assert blended is not None and _avg(pct) is not None
    assert blended > _avg(pct)


def test_recent_calm_pulls_forecast_below_average() -> None:
    pct, reset = 10.0, 14_400
    blended = compute_forecast(pct, reset, FORECAST_WINDOW_5H_SECONDS, None, 0.0)
    assert blended is not None and _avg(pct) is not None
    assert blended < _avg(pct)  # recent zero burn → projection eases off


def test_blended_eta_consistent_with_forecast() -> None:
    pct, reset = 10.0, 14_400
    hot = 0.01
    fc = compute_forecast(pct, reset, FORECAST_WINDOW_5H_SECONDS, None, hot)
    eta = compute_limit_eta(pct, reset, FORECAST_WINDOW_5H_SECONDS, None, hot)
    assert fc is not None and fc >= 100
    assert eta is not None and eta <= reset  # hits the wall before reset

    cold_fc = compute_forecast(pct, reset, FORECAST_WINDOW_5H_SECONDS, None, 0.0)
    cold_eta = compute_limit_eta(pct, reset, FORECAST_WINDOW_5H_SECONDS, None, 0.0)
    assert cold_fc is not None and cold_fc < 100
    assert cold_eta is None  # never reaches the wall before reset
