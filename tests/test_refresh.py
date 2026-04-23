from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths
from claude_rotate.oauth import TokenPair
from claude_rotate.refresh import ensure_fresh


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


NOW = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)


def _acc(**overrides) -> Account:
    fields = {
        "name": "main",
        "runtime_token": "sk-ant-oat01-OLD" + "a" * 97,
        "label": "Max-20 main",
        "created_at": NOW - timedelta(days=5),
        "plan": "max_20x",
        "refresh_token": "sk-ant-ort01-" + "b" * 100,
        "runtime_token_obtained_at": NOW - timedelta(hours=6),  # stale (>4h)
        "refresh_token_obtained_at": NOW - timedelta(days=5),
    }
    fields.update(overrides)
    return Account(**fields)


def test_ensure_fresh_refreshes_stale_token(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    acct = _acc()
    Store(p).save({"main": acct})

    new_pair = TokenPair(
        access_token="sk-ant-oat01-NEW" + "c" * 97,
        refresh_token="sk-ant-ort01-NEW" + "d" * 97,
        expires_in=28800,
        scope=(
            "user:profile user:inference user:sessions:claude_code "
            "user:mcp_servers user:file_upload"
        ),
        obtained_at=NOW,
    )

    with patch("claude_rotate.refresh.refresh_access_token", return_value=new_pair) as mock_refresh:
        fresh = ensure_fresh(acct, p, now=NOW)

    mock_refresh.assert_called_once_with(acct.refresh_token)
    assert fresh.runtime_token == new_pair.access_token
    assert fresh.refresh_token == new_pair.refresh_token
    assert fresh.runtime_token_obtained_at == NOW
    assert fresh.refresh_token_obtained_at == NOW

    reloaded = Store(p).load()["main"]
    assert reloaded.runtime_token == new_pair.access_token
    assert reloaded.refresh_token == new_pair.refresh_token


def test_ensure_fresh_skips_when_recent(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    acct = _acc(runtime_token_obtained_at=NOW - timedelta(minutes=10))
    Store(p).save({"main": acct})

    with patch("claude_rotate.refresh.refresh_access_token") as mock_refresh:
        fresh = ensure_fresh(acct, p, now=NOW)

    mock_refresh.assert_not_called()
    assert fresh is acct  # unchanged, same instance


def test_ensure_fresh_skips_ci_path(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    acct = _acc(refresh_token=None, runtime_token_obtained_at=NOW - timedelta(days=30))
    Store(p).save({"main": acct})

    with patch("claude_rotate.refresh.refresh_access_token") as mock_refresh:
        fresh = ensure_fresh(acct, p, now=NOW)

    mock_refresh.assert_not_called()
    assert fresh is acct


def test_ensure_fresh_returns_unchanged_on_refresh_http_error(tmp_path) -> None:
    """If refresh fails (revoked token, network error), return the original
    account. The caller will exec anyway so the user sees claude's own
    login prompt, not a rotator crash."""
    from claude_rotate.errors import ClaudeRotateError

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    acct = _acc()
    Store(p).save({"main": acct})

    with patch(
        "claude_rotate.refresh.refresh_access_token",
        side_effect=ClaudeRotateError("HTTP 400 invalid_grant"),
    ):
        fresh = ensure_fresh(acct, p, now=NOW)

    assert fresh is acct
    # store not updated
    assert Store(p).load()["main"].runtime_token == acct.runtime_token


def test_ensure_fresh_swallows_network_errors(tmp_path) -> None:
    """DNS / socket / URL errors must not escape — user would see a
    traceback instead of a graceful fallthrough."""
    import urllib.error

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    acct = _acc()
    Store(p).save({"main": acct})

    for err in (
        urllib.error.URLError("DNS failure"),
        TimeoutError("connect timed out"),
        ConnectionRefusedError("connection refused"),
    ):
        with patch(
            "claude_rotate.refresh.refresh_access_token",
            side_effect=err,
        ):
            fresh = ensure_fresh(acct, p, now=NOW)
        assert fresh is acct
        assert Store(p).load()["main"].runtime_token == acct.runtime_token
