from __future__ import annotations

import json
from pathlib import Path

from claude_rotate.probe import ProbeResult, parse_usage_response

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
    assert r.w7_opus_pct is None  # seven_day_opus is null in fixture
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
    assert r.w7_opus_pct is None


def test_parse_usage_response_ignores_unconsumed_buckets() -> None:
    """Buckets we do not model must not derail the ones we do."""
    body: dict = {
        "five_hour": {"utilization": 0.0, "resets_at": None},
        "seven_day": {"utilization": 50.0, "resets_at": None},
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "extra_usage": {"is_enabled": True, "currency": "usd", "monthly_limit": 100},
    }
    r = parse_usage_response(200, body, now=_NOW)
    assert r.ok
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


# ---------------------------------------------------------------------------
# merge_opus_usage: overlay per-model buckets onto an inference-header probe
# ---------------------------------------------------------------------------


def test_merge_opus_usage_takes_buckets_from_oauth_keeps_unified() -> None:
    from claude_rotate.probe import merge_opus_usage

    base = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=100, w7_reset_secs=200
    )
    oauth = ProbeResult(
        ok=True,
        http_code=200,
        h5_pct=11.0,
        w7_pct=22.0,
        h5_reset_secs=99,
        w7_reset_secs=199,
        w7_opus_pct=44.0,
    )
    merged = merge_opus_usage(base, oauth)
    # Unified numbers stay from the headers (exact); only the buckets the
    # headers cannot provide come from the OAuth endpoint.
    assert merged.h5_pct == 10.0
    assert merged.w7_pct == 20.0
    assert merged.h5_reset_secs == 100
    assert merged.w7_reset_secs == 200
    assert merged.w7_opus_pct == 44.0


def test_merge_opus_usage_failed_or_missing_oauth_returns_base() -> None:
    from claude_rotate.probe import merge_opus_usage

    base = ProbeResult(ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0)
    assert merge_opus_usage(base, None) is base
    assert merge_opus_usage(base, ProbeResult(ok=False, http_code=500)) is base


def test_probe_many_carries_opus_pct_into_candidate(monkeypatch) -> None:
    from datetime import UTC, datetime

    from claude_rotate import probe
    from claude_rotate.accounts import Account

    account = Account(
        name="main",
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label="main",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        plan="max_20x",
    )
    monkeypatch.setattr(
        probe,
        "fetch_usage",
        lambda token: ProbeResult(
            ok=True, http_code=200, h5_pct=1.0, w7_pct=2.0, h5_reset_secs=10, w7_reset_secs=20
        ),
    )
    monkeypatch.setattr(
        probe,
        "fetch_oauth_usage",
        lambda token: ProbeResult(ok=True, http_code=200, w7_opus_pct=66.0),
    )
    cands = probe.probe_many([account])
    assert cands[0].w7_opus_pct == 66.0
    assert cands[0].h5_pct == 1.0


def test_probe_many_failed_base_probe_skips_oauth_call(monkeypatch) -> None:
    from datetime import UTC, datetime

    from claude_rotate import probe
    from claude_rotate.accounts import Account

    account = Account(
        name="main",
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label="main",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        plan="max_20x",
    )
    monkeypatch.setattr(
        probe,
        "fetch_usage",
        lambda token: ProbeResult(ok=False, http_code=401, error="unauthorized"),
    )

    def _boom(token: str) -> ProbeResult:
        raise AssertionError("oauth usage must not be fetched for a failed base probe")

    monkeypatch.setattr(probe, "fetch_oauth_usage", _boom)
    cands = probe.probe_many([account])
    assert cands[0].probe_error == "unauthorized"
    assert cands[0].w7_opus_pct is None


# ---------------------------------------------------------------------------
# limits array: model-scoped weekly windows (e.g. Fable's separate weekly cap)
# ---------------------------------------------------------------------------


def test_parse_usage_response_scoped_limits() -> None:
    from claude_rotate.selection import ScopedLimit

    body: dict = {
        "five_hour": {"utilization": 9.0, "resets_at": None},
        "seven_day": {"utilization": 31.0, "resets_at": None},
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "extra_usage": None,
        "limits": [
            {"kind": "session", "group": "session", "percent": 9, "resets_at": None},
            {"kind": "weekly_all", "group": "weekly", "percent": 31, "resets_at": None},
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 35,
                "resets_at": "2026-07-03T21:59:59+00:00",
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                "is_active": True,
            },
        ],
    }
    from datetime import UTC, datetime

    reset_epoch = int(datetime(2026, 7, 3, 21, 59, 59, tzinfo=UTC).timestamp())
    r = parse_usage_response(200, body, now=reset_epoch - 600)
    assert r.w7_scoped == (ScopedLimit(label="fable", pct=35.0, reset_secs=600),)


def test_parse_usage_response_scoped_limits_skips_incomplete_entries() -> None:
    body: dict = {
        "five_hour": {"utilization": 1.0, "resets_at": None},
        "seven_day": {"utilization": 2.0, "resets_at": None},
        "limits": [
            # No percent → skipped
            {"kind": "weekly_scoped", "scope": {"model": {"display_name": "Fable"}}},
            # No scope label at all → skipped
            {"kind": "weekly_scoped", "percent": 10, "scope": {"model": None, "surface": None}},
            # Surface scope (no model) → label falls back to the surface
            {"kind": "weekly_scoped", "percent": 20, "scope": {"model": None, "surface": "code"}},
        ],
    }
    r = parse_usage_response(200, body, now=_NOW)
    assert len(r.w7_scoped) == 1
    assert r.w7_scoped[0].label == "code"
    assert r.w7_scoped[0].pct == 20.0


def test_parse_usage_response_no_limits_array() -> None:
    """Older responses without a limits array → empty scoped tuple."""
    body: dict = {
        "five_hour": {"utilization": 1.0, "resets_at": None},
        "seven_day": {"utilization": 2.0, "resets_at": None},
    }
    r = parse_usage_response(200, body, now=_NOW)
    assert r.w7_scoped == ()


def test_merge_opus_usage_carries_scoped_limits() -> None:
    from claude_rotate.probe import merge_opus_usage
    from claude_rotate.selection import ScopedLimit

    base = ProbeResult(ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0)
    scoped = (ScopedLimit(label="fable", pct=35.0, reset_secs=600),)
    oauth = ProbeResult(ok=True, http_code=200, w7_scoped=scoped)
    assert merge_opus_usage(base, oauth).w7_scoped == scoped
