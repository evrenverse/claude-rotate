from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths
from claude_rotate.metadata import refresh_stale_accounts
from claude_rotate.probe import ProbeResult


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def _acc(
    name: str,
    refreshed_at: datetime | None,
) -> Account:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    return Account(
        name=name,
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label=name,
        created_at=now,
        plan="max_20x",
        metadata_refreshed_at=refreshed_at,
    )


def test_refresh_skips_fresh_accounts(tmp_path) -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save(
        {
            "fresh": _acc("fresh", now - timedelta(days=2)),
        }
    )

    with patch("claude_rotate.metadata.fetch_usage") as mock:
        refresh_stale_accounts(p, now=now)
    mock.assert_not_called()


def test_refresh_triggers_for_stale_account(tmp_path) -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"stale": _acc("stale", now - timedelta(days=10))})

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with patch("claude_rotate.metadata.fetch_usage", return_value=probe_ok):
        refresh_stale_accounts(p, now=now)

    loaded = Store(p).load()["stale"]
    assert loaded.metadata_refreshed_at == now


def test_refresh_failure_does_not_update_metadata(tmp_path) -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    old_ts = now - timedelta(days=10)
    Store(p).save({"stale": _acc("stale", old_ts)})

    probe_fail = ProbeResult(ok=False, http_code=0, error="timeout")
    with patch("claude_rotate.metadata.fetch_usage", return_value=probe_fail):
        refresh_stale_accounts(p, now=now)

    loaded = Store(p).load()["stale"]
    assert loaded.metadata_refreshed_at == old_ts


def test_refresh_treats_null_refreshed_at_as_stale(tmp_path) -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"never": _acc("never", None)})

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=5.0, w7_pct=10.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with patch("claude_rotate.metadata.fetch_usage", return_value=probe_ok) as mock:
        refresh_stale_accounts(p, now=now)
    mock.assert_called_once()


def test_refresh_unauthorized_probe_does_not_update(tmp_path) -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    old_ts = now - timedelta(days=10)
    Store(p).save({"bad": _acc("bad", old_ts)})

    probe_unauth = ProbeResult(ok=False, http_code=401, error="unauthorized")
    with patch("claude_rotate.metadata.fetch_usage", return_value=probe_unauth):
        refresh_stale_accounts(p, now=now)

    loaded = Store(p).load()["bad"]
    assert loaded.metadata_refreshed_at == old_ts


def test_refresh_updates_subscription_status_for_oauth_account(tmp_path) -> None:
    """Accounts with refresh_token: metadata refresh also re-fetches profile info."""
    now = datetime(2026, 4, 22, tzinfo=UTC)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    old_ts = now - timedelta(days=10)
    # Account has a refresh_token (OAuth-installed)
    acc = Account(
        name="oauth",
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label="oauth",
        created_at=now,
        plan="max_20x",
        metadata_refreshed_at=old_ts,
        refresh_token="sk-ant-ort01-refreshtoken",
        subscription_status=None,
    )
    Store(p).save({"oauth": acc})

    from claude_rotate.oauth import ProfileInfo
    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    profile_ok = ProfileInfo(
        ok=True,
        email="oauth@example.com",
        rate_limit_tier="claude_max_20x",
        subscription_status="active",
        subscription_created_at="2025-11-15T00:00:00Z",
    )
    with (
        patch("claude_rotate.metadata.fetch_usage", return_value=probe_ok),
        patch("claude_rotate.oauth.fetch_profile", return_value=profile_ok),
    ):
        refresh_stale_accounts(p, now=now)

    loaded = Store(p).load()["oauth"]
    assert loaded.metadata_refreshed_at == now
    assert loaded.subscription_status == "active"


def test_refresh_saves_only_when_something_changed(tmp_path) -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    # All accounts are fresh
    Store(p).save({"fresh": _acc("fresh", now - timedelta(days=1))})

    with patch("claude_rotate.metadata.fetch_usage") as mock_probe:
        refresh_stale_accounts(p, now=now)

    mock_probe.assert_not_called()
