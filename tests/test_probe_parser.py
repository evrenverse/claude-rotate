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
        w7_sonnet_pct=33.0,
        w7_opus_pct=44.0,
        extra_usage_enabled=True,
    )
    merged = merge_opus_usage(base, oauth)
    # Unified numbers stay from the headers (exact); only the buckets the
    # headers cannot provide come from the OAuth endpoint.
    assert merged.h5_pct == 10.0
    assert merged.w7_pct == 20.0
    assert merged.h5_reset_secs == 100
    assert merged.w7_reset_secs == 200
    assert merged.w7_sonnet_pct == 33.0
    assert merged.w7_opus_pct == 44.0
    assert merged.extra_usage_enabled is True


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
