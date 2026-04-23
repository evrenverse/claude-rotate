from __future__ import annotations

import json
from pathlib import Path

from claude_rotate.probe import ProbeResult, parse_usage_response  # noqa: F401

FIX = Path(__file__).parent / "fixtures"

# Fixed timestamp for deterministic tests; usage fixture resets_at values are
# set relative to this epoch second.
_NOW = 1_776_854_321


def test_parse_usage_response_ok() -> None:
    body = json.loads((FIX / "usage_max20.json").read_text())
    # Use a now value before the resets_at timestamps so secs > 0
    r = parse_usage_response(200, body, now=_NOW)
    assert r.ok
    assert r.http_code == 200
    assert r.h5_pct == 8.0
    assert r.w7_pct == 89.0
    assert r.w7_sonnet_pct == 21.0
    assert r.w7_opus_pct is None  # seven_day_opus is null in fixture
    assert r.extra_usage_enabled is False
    assert r.h5_reset_secs > 0
    assert r.w7_reset_secs > 0


def test_parse_usage_response_null_buckets() -> None:
    """All buckets null → h5_pct and w7_pct are None (graceful)."""
    body: dict = {
        "five_hour": None,
        "seven_day": None,
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "extra_usage": None,
    }
    r = parse_usage_response(200, body, now=_NOW)
    assert r.ok
    assert r.h5_pct is None
    assert r.w7_pct is None
    assert r.w7_sonnet_pct is None
    assert r.w7_opus_pct is None
    assert r.extra_usage_enabled is False


def test_parse_usage_response_extra_usage_enabled() -> None:
    body: dict = {
        "five_hour": {"utilization": 0.0, "resets_at": None},
        "seven_day": {"utilization": 50.0, "resets_at": None},
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "extra_usage": {"is_enabled": True, "currency": "usd", "monthly_limit": 100},
    }
    r = parse_usage_response(200, body, now=_NOW)
    assert r.ok
    assert r.extra_usage_enabled is True
    assert r.w7_pct == 50.0


def test_parse_usage_response_zero_reset_gives_zero_secs() -> None:
    """resets_at=None → 0 seconds (already elapsed)."""
    body: dict = {
        "five_hour": {"utilization": 30.0, "resets_at": None},
        "seven_day": {"utilization": 60.0, "resets_at": None},
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "extra_usage": {},
    }
    r = parse_usage_response(200, body, now=_NOW)
    assert r.h5_reset_secs == 0
    assert r.w7_reset_secs == 0
