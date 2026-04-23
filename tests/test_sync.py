from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths
from claude_rotate.credentials_file import CredentialsPayload
from claude_rotate.sync import (
    CurrentSession,
    read_current_session,
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
