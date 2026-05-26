from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths, paths
from claude_rotate.credentials_file import CredentialsPayload, write_credentials
from claude_rotate.sync import (
    CurrentSession,
    read_current_session,
    reconcile_isolated,
    reconcile_once,
    write_current_session,
)

NOW = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)


def _paths(tmp_path: Path) -> Paths:
    p = Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )
    p.config_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    return p


def _acc(name: str = "sub1", at: str = "old", rt: str = "old") -> Account:
    return Account(
        name=name,
        runtime_token=f"sk-ant-oat01-{at}" + "a" * 90,
        label=f"Max-20 {name}",
        created_at=NOW - timedelta(days=2),
        plan="max_20x",
        refresh_token=f"sk-ant-ort01-{rt}" + "b" * 90,
        runtime_token_obtained_at=NOW - timedelta(hours=1),
        refresh_token_obtained_at=NOW - timedelta(days=2),
    )


def _payload(at: str, rt: str) -> CredentialsPayload:
    return CredentialsPayload(
        access_token=f"sk-ant-oat01-{at}" + "a" * 90,
        refresh_token=f"sk-ant-ort01-{rt}" + "b" * 90,
        expires_at_ms=int((NOW + timedelta(hours=8)).timestamp() * 1000),
        scopes=[
            "user:profile",
            "user:inference",
            "user:sessions:claude_code",
            "user:mcp_servers",
            "user:file_upload",
        ],
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
    )


def test_current_session_roundtrip(tmp_path) -> None:
    p = _paths(tmp_path)
    session = CurrentSession(account_name="sub1")
    write_current_session(p, session)
    assert read_current_session(p) == session


def test_current_session_missing_returns_none(tmp_path) -> None:
    p = _paths(tmp_path)
    assert read_current_session(p) is None


def test_reconcile_noop_when_tokens_match(tmp_path) -> None:
    p = _paths(tmp_path)
    acct = _acc(at="match", rt="match")
    Store(p).save({"sub1": acct})
    write_current_session(p, CurrentSession(account_name="sub1"))

    payload = _payload(at="match", rt="match")
    changed = reconcile_once(payload, p, now=NOW)
    assert changed is False


def test_reconcile_updates_when_access_token_rotated(tmp_path) -> None:
    p = _paths(tmp_path)
    acct = _acc(at="old", rt="same")
    Store(p).save({"sub1": acct})
    write_current_session(p, CurrentSession(account_name="sub1"))

    payload = _payload(at="new", rt="same")
    changed = reconcile_once(payload, p, now=NOW)
    assert changed is True

    reloaded = Store(p).load()["sub1"]
    assert reloaded.runtime_token == payload.access_token
    assert reloaded.refresh_token == payload.refresh_token
    assert reloaded.runtime_token_obtained_at == NOW
    assert reloaded.refresh_token_obtained_at == acct.refresh_token_obtained_at


def test_reconcile_updates_when_refresh_token_rotated(tmp_path) -> None:
    p = _paths(tmp_path)
    acct = _acc(at="same", rt="old")
    Store(p).save({"sub1": acct})
    write_current_session(p, CurrentSession(account_name="sub1"))

    payload = _payload(at="same", rt="new")
    changed = reconcile_once(payload, p, now=NOW)
    assert changed is True

    reloaded = Store(p).load()["sub1"]
    assert reloaded.refresh_token == payload.refresh_token
    assert reloaded.refresh_token_obtained_at == NOW


def test_reconcile_skips_when_no_current_session(tmp_path) -> None:
    p = _paths(tmp_path)
    Store(p).save({"sub1": _acc()})
    # no write_current_session

    payload = _payload(at="rotated", rt="rotated")
    changed = reconcile_once(payload, p, now=NOW)
    assert changed is False


def test_reconcile_skips_when_account_deleted(tmp_path) -> None:
    p = _paths(tmp_path)
    Store(p).save({"sub2": _acc(name="sub2")})
    write_current_session(p, CurrentSession(account_name="sub1"))  # stale pointer

    payload = _payload(at="anything", rt="anything")
    changed = reconcile_once(payload, p, now=NOW)
    assert changed is False


# ---------------------------------------------------------------------------
# refresh_stale_tokens — proactive token refresh during idle periods
# ---------------------------------------------------------------------------


def test_refresh_stale_skips_fresh_account(tmp_path) -> None:
    from unittest.mock import patch

    from claude_rotate.sync import refresh_stale_tokens

    p = _paths(tmp_path)
    Store(p).save({"sub1": _acc()})  # runtime_token_obtained_at = NOW - 1h → fresh

    with patch("claude_rotate.sync.refresh_access_token") as mock_refresh:
        refreshed = refresh_stale_tokens(p, now=NOW)

    mock_refresh.assert_not_called()
    assert refreshed == []


def test_refresh_stale_refreshes_stale_account_and_updates_store(tmp_path) -> None:
    from unittest.mock import patch

    from claude_rotate.oauth import TokenPair
    from claude_rotate.sync import refresh_stale_tokens

    p = _paths(tmp_path)
    stale = Account(
        name="sub1",
        runtime_token="sk-ant-oat01-old" + "a" * 89,
        label="Max-20 sub1",
        created_at=NOW - timedelta(days=3),
        plan="max_20x",
        refresh_token="sk-ant-ort01-old" + "b" * 89,
        runtime_token_obtained_at=NOW - timedelta(hours=8),  # stale
        refresh_token_obtained_at=NOW - timedelta(days=3),
    )
    Store(p).save({"sub1": stale})

    new_pair = TokenPair(
        access_token="sk-ant-oat01-new" + "c" * 89,
        refresh_token="sk-ant-ort01-new" + "d" * 89,
        expires_in=28800,
        scope="user:inference",
        obtained_at=NOW,
    )
    with patch("claude_rotate.sync.refresh_access_token", return_value=new_pair):
        refreshed = refresh_stale_tokens(p, now=NOW)

    assert refreshed == ["sub1"]
    reloaded = Store(p).load()["sub1"]
    assert reloaded.runtime_token == new_pair.access_token
    assert reloaded.refresh_token == new_pair.refresh_token
    assert reloaded.runtime_token_obtained_at == NOW
    assert reloaded.refresh_token_obtained_at == NOW


def test_refresh_stale_skips_ci_accounts(tmp_path) -> None:
    from unittest.mock import patch

    from claude_rotate.sync import refresh_stale_tokens

    p = _paths(tmp_path)
    ci = Account(
        name="ci",
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label="ci",
        created_at=NOW - timedelta(days=30),
        plan="unknown",
        refresh_token=None,  # CI path
        runtime_token_obtained_at=NOW - timedelta(days=30),
        refresh_token_obtained_at=None,
    )
    Store(p).save({"ci": ci})

    with patch("claude_rotate.sync.refresh_access_token") as mock_refresh:
        refreshed = refresh_stale_tokens(p, now=NOW)

    mock_refresh.assert_not_called()
    assert refreshed == []


def test_refresh_stale_swallows_errors_per_account(tmp_path) -> None:
    """One dead refresh_token should not block the others."""
    from unittest.mock import patch

    from claude_rotate.errors import ClaudeRotateError
    from claude_rotate.oauth import TokenPair
    from claude_rotate.sync import refresh_stale_tokens

    p = _paths(tmp_path)
    stale_a = Account(
        name="a",
        runtime_token="sk-ant-oat01-a" + "a" * 95,
        label="a",
        created_at=NOW - timedelta(days=3),
        plan="max_20x",
        refresh_token="sk-ant-ort01-dead" + "a" * 88,
        runtime_token_obtained_at=NOW - timedelta(hours=8),
        refresh_token_obtained_at=NOW - timedelta(days=3),
    )
    stale_b = Account(
        name="b",
        runtime_token="sk-ant-oat01-b" + "b" * 95,
        label="b",
        created_at=NOW - timedelta(days=3),
        plan="max_20x",
        refresh_token="sk-ant-ort01-live" + "b" * 88,
        runtime_token_obtained_at=NOW - timedelta(hours=8),
        refresh_token_obtained_at=NOW - timedelta(days=3),
    )
    Store(p).save({"a": stale_a, "b": stale_b})

    new_pair = TokenPair(
        access_token="sk-ant-oat01-new" + "n" * 89,
        refresh_token="sk-ant-ort01-new" + "n" * 89,
        expires_in=28800,
        scope="user:inference",
        obtained_at=NOW,
    )

    def fake_refresh(token: str):
        if "dead" in token:
            raise ClaudeRotateError("HTTP 400 invalid_grant")
        return new_pair

    with patch("claude_rotate.sync.refresh_access_token", side_effect=fake_refresh):
        refreshed = refresh_stale_tokens(p, now=NOW)

    assert refreshed == ["b"]
    loaded = Store(p).load()
    # a unchanged (refresh failed)
    assert loaded["a"].runtime_token == stale_a.runtime_token
    # b updated
    assert loaded["b"].runtime_token == new_pair.access_token


def test_refresh_stale_rewrites_credentials_json_for_active_session(tmp_path, monkeypatch) -> None:
    """If the current session's account is refreshed, ~/.claude/.credentials.json
    must be rewritten so the running claude and the next pre-run reconcile
    both see the fresh tokens."""
    import json as _json
    from unittest.mock import patch

    from claude_rotate.oauth import TokenPair
    from claude_rotate.sync import refresh_stale_tokens

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    p = _paths(tmp_path)
    stale = Account(
        name="sub1",
        runtime_token="sk-ant-oat01-old" + "a" * 89,
        label="Max-20 sub1",
        created_at=NOW - timedelta(days=3),
        plan="max_20x",
        refresh_token="sk-ant-ort01-old" + "b" * 89,
        runtime_token_obtained_at=NOW - timedelta(hours=8),
        refresh_token_obtained_at=NOW - timedelta(days=3),
    )
    Store(p).save({"sub1": stale})
    write_current_session(p, CurrentSession(account_name="sub1"))

    new_pair = TokenPair(
        access_token="sk-ant-oat01-new" + "c" * 89,
        refresh_token="sk-ant-ort01-new" + "d" * 89,
        expires_in=28800,
        scope="user:inference",
        obtained_at=NOW,
    )
    with patch("claude_rotate.sync.refresh_access_token", return_value=new_pair):
        refreshed = refresh_stale_tokens(p, now=NOW)

    assert refreshed == ["sub1"]
    creds_path = home / ".claude" / ".credentials.json"
    assert creds_path.exists()
    creds = _json.loads(creds_path.read_text())
    assert creds["claudeAiOauth"]["accessToken"] == new_pair.access_token
    assert creds["claudeAiOauth"]["refreshToken"] == new_pair.refresh_token


# ---------------------------------------------------------------------------
# reconcile_isolated — sync per-account dirs back to accounts.json
# ---------------------------------------------------------------------------


def _seed_account(p: Paths, name: str, token: str) -> None:
    store = Store(p)
    accts = store.load()
    accts[name] = Account(
        name=name,
        runtime_token=token,
        label=name,
        created_at=datetime(2026, 4, 23, tzinfo=UTC),
        plan="max_20x",
        refresh_token="sk-ant-ort01-OLD",
        runtime_token_obtained_at=datetime(2026, 4, 23, tzinfo=UTC),
        refresh_token_obtained_at=datetime(2026, 4, 23, tzinfo=UTC),
    )
    store.save(accts)


def test_reconcile_isolated_picks_up_rotated_token(rotate_dir: Path) -> None:
    p = paths()
    _seed_account(p, "matri", "sk-ant-oat01-OLD")
    cfg_dir = p.account_configs_dir / "matri"
    cfg_dir.mkdir(parents=True)
    write_credentials(
        CredentialsPayload(
            access_token="sk-ant-oat01-NEW",
            refresh_token="sk-ant-ort01-NEW",
            expires_at_ms=1_700_000_000_000,
            scopes=["user:inference"],
            subscription_type="max",
            rate_limit_tier=None,
        ),
        config_dir=cfg_dir,
    )

    changed = reconcile_isolated(p, now=datetime(2026, 5, 25, tzinfo=UTC))
    assert changed == ["matri"]
    acct = Store(p).load()["matri"]
    assert acct.runtime_token == "sk-ant-oat01-NEW"
    assert acct.refresh_token == "sk-ant-ort01-NEW"


def test_reconcile_isolated_noop_when_unchanged(rotate_dir: Path) -> None:
    p = paths()
    _seed_account(p, "matri", "sk-ant-oat01-SAME")
    cfg_dir = p.account_configs_dir / "matri"
    cfg_dir.mkdir(parents=True)
    write_credentials(
        CredentialsPayload(
            access_token="sk-ant-oat01-SAME",
            refresh_token="sk-ant-ort01-OLD",
            expires_at_ms=1_700_000_000_000,
            scopes=["user:inference"],
            subscription_type="max",
            rate_limit_tier=None,
        ),
        config_dir=cfg_dir,
    )
    assert reconcile_isolated(p, now=datetime(2026, 5, 25, tzinfo=UTC)) == []


def test_reconcile_isolated_skips_stale_file_no_rollback(rotate_dir: Path) -> None:
    """A per-account .credentials.json OLDER than accounts.json must NOT be
    copied back. Doing so rolls accounts.json onto an already-rotated, dead
    refresh token — the bug behind the constant relogins."""
    import os

    p = paths()
    fresh_obtained = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)
    Store(p).save(
        {
            "matri": Account(
                name="matri",
                runtime_token="sk-ant-oat01-FRESH",
                label="matri",
                created_at=datetime(2026, 4, 23, tzinfo=UTC),
                plan="max_20x",
                refresh_token="sk-ant-ort01-FRESH",
                runtime_token_obtained_at=fresh_obtained,
                refresh_token_obtained_at=fresh_obtained,
            )
        }
    )
    cfg_dir = p.account_configs_dir / "matri"
    cfg_dir.mkdir(parents=True)
    write_credentials(
        CredentialsPayload(
            access_token="sk-ant-oat01-STALE",
            refresh_token="sk-ant-ort01-STALE",
            expires_at_ms=1_700_000_000_000,
            scopes=["user:inference"],
            subscription_type="max",
            rate_limit_tier=None,
        ),
        config_dir=cfg_dir,
    )
    # Backdate the file so it predates accounts.json's obtained_at (stale leftover).
    stale_ts = (fresh_obtained - timedelta(hours=6)).timestamp()
    os.utime(cfg_dir / ".credentials.json", (stale_ts, stale_ts))

    changed = reconcile_isolated(p, now=datetime(2026, 5, 26, 12, 5, tzinfo=UTC))
    assert changed == []
    acct = Store(p).load()["matri"]
    assert acct.runtime_token == "sk-ant-oat01-FRESH"  # NOT rolled back
    assert acct.refresh_token == "sk-ant-ort01-FRESH"


def test_refresh_stale_tokens_isolated_writes_config_dir(rotate_dir: Path) -> None:
    """Isolation mode: a stale idle account is refreshed into its own config dir."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from claude_rotate.credentials_file import CredentialsFile
    from claude_rotate.sync import refresh_stale_tokens

    p = paths()
    old = datetime(2026, 1, 1, tzinfo=UTC)
    Store(p).save(
        {
            "matri": Account(
                name="matri",
                runtime_token="oat-OLD",
                label="matri",
                created_at=old,
                plan="max_20x",
                refresh_token="ort-OLD",
                runtime_token_obtained_at=old,
                refresh_token_obtained_at=old,
            )
        }
    )
    cfg_dir = p.account_configs_dir / "matri"
    cfg_dir.mkdir(parents=True)

    fake = SimpleNamespace(access_token="oat-NEW", refresh_token="ort-NEW")
    with patch("claude_rotate.sync.refresh_access_token", return_value=fake):
        refreshed = refresh_stale_tokens(p, now=datetime(2026, 5, 25, tzinfo=UTC), isolated=True)

    assert refreshed == ["matri"]
    assert Store(p).load()["matri"].runtime_token == "oat-NEW"
    written = CredentialsFile(cfg_dir).read()
    assert written is not None
    assert written.access_token == "oat-NEW"
