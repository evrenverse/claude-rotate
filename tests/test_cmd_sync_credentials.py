from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_rotate.accounts import Account, Store
from claude_rotate.commands.sync_credentials import execute
from claude_rotate.config import Paths
from claude_rotate.credentials_file import CredentialsFile, CredentialsPayload
from claude_rotate.sync import CurrentSession, write_current_session


def _paths(tmp_path: Path) -> Paths:
    p = Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )
    p.config_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    return p


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_sync_credentials_returns_zero_when_nothing_to_do(tmp_path, monkeypatch) -> None:
    _fake_home(tmp_path, monkeypatch)
    p = _paths(tmp_path)
    rc = execute(p)
    assert rc == 0


def test_sync_credentials_updates_accounts_json(tmp_path, monkeypatch) -> None:
    _fake_home(tmp_path, monkeypatch)
    p = _paths(tmp_path)

    now = datetime.now(UTC)
    acct = Account(
        name="sub1",
        runtime_token="sk-ant-oat01-OLD" + "a" * 90,
        label="Max-20 sub1",
        created_at=now - timedelta(days=1),
        plan="max_20x",
        refresh_token="sk-ant-ort01-OLD" + "b" * 90,
        runtime_token_obtained_at=now - timedelta(hours=8),
        refresh_token_obtained_at=now - timedelta(days=1),
    )
    Store(p).save({"sub1": acct})
    write_current_session(p, CurrentSession(account_name="sub1"))

    CredentialsFile().write(
        CredentialsPayload(
            access_token="sk-ant-oat01-NEW" + "c" * 90,
            refresh_token="sk-ant-ort01-NEW" + "d" * 90,
            expires_at_ms=int((now + timedelta(hours=8)).timestamp() * 1000),
            scopes=["user:inference", "user:sessions:claude_code"],
            subscription_type="max",
            rate_limit_tier="default_claude_max_20x",
        )
    )

    rc = execute(p)
    assert rc == 0

    reloaded = Store(p).load()["sub1"]
    assert reloaded.runtime_token.startswith("sk-ant-oat01-NEW")
    assert reloaded.refresh_token.startswith("sk-ant-ort01-NEW")
