from __future__ import annotations

from datetime import UTC, datetime, timedelta

from claude_rotate.accounts import Account
from claude_rotate.refresh_policy import should_refresh

NOW = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)


def _acc(**kwargs) -> Account:
    return Account(
        name="test",
        runtime_token="sk-ant-oat01-" + "a" * 100,
        label="Test",
        created_at=NOW,
        **kwargs,
    )


def test_refresh_when_no_obtained_at() -> None:
    """Missing obtained_at (legacy v7 account) means we don't know the age → refresh."""
    acct = _acc(refresh_token="sk-ant-ort01-" + "b" * 100)
    assert should_refresh(acct, now=NOW) is True


def test_no_refresh_when_token_is_fresh() -> None:
    """Token obtained <4h ago is still fresh."""
    acct = _acc(
        refresh_token="sk-ant-ort01-" + "b" * 100,
        runtime_token_obtained_at=NOW - timedelta(hours=2),
    )
    assert should_refresh(acct, now=NOW) is False


def test_refresh_when_token_is_stale() -> None:
    """Token older than threshold → refresh."""
    acct = _acc(
        refresh_token="sk-ant-ort01-" + "b" * 100,
        runtime_token_obtained_at=NOW - timedelta(hours=5),
    )
    assert should_refresh(acct, now=NOW) is True


def test_no_refresh_when_ci_path_no_refresh_token() -> None:
    """CI-path accounts have no refresh_token and cannot be refreshed."""
    acct = _acc(
        refresh_token=None,
        runtime_token_obtained_at=NOW - timedelta(days=30),
    )
    assert should_refresh(acct, now=NOW) is False


def test_threshold_boundary_exactly_four_hours() -> None:
    """At exactly the threshold, we refresh (inclusive)."""
    acct = _acc(
        refresh_token="sk-ant-ort01-" + "b" * 100,
        runtime_token_obtained_at=NOW - timedelta(hours=4),
    )
    assert should_refresh(acct, now=NOW) is True
