from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_rotate.accounts import Account, Store
from claude_rotate.commands.sync_credentials import execute
from claude_rotate.config import Paths
from claude_rotate.credentials_file import CredentialsFile, CredentialsPayload
from claude_rotate.settings import RotateConfig, save_config
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


def test_sync_credentials_isolated_path(rotate_dir: Path) -> None:
    from datetime import UTC, datetime

    from claude_rotate.accounts import Account, Store
    from claude_rotate.commands import sync_credentials
    from claude_rotate.config import paths
    from claude_rotate.credentials_file import CredentialsPayload, write_credentials
    from claude_rotate.settings import RotateConfig, save_config

    p = paths()
    save_config(p, RotateConfig(session_isolation=True))
    store = Store(p)
    store.save(
        {
            "matri": Account(
                name="matri",
                runtime_token="sk-ant-oat01-OLD",
                label="matri",
                created_at=datetime(2026, 4, 23, tzinfo=UTC),
                plan="max_20x",
                refresh_token="r-OLD",
                runtime_token_obtained_at=datetime(2026, 4, 23, tzinfo=UTC),
                refresh_token_obtained_at=datetime(2026, 4, 23, tzinfo=UTC),
            )
        }
    )
    cfg_dir = p.account_configs_dir / "matri"
    cfg_dir.mkdir(parents=True)
    write_credentials(
        CredentialsPayload(
            "sk-ant-oat01-NEW", "r-NEW", 1_700_000_000_000, ["user:inference"], "max", None
        ),
        config_dir=cfg_dir,
    )

    assert sync_credentials.execute(p) == 0
    assert Store(p).load()["matri"].runtime_token == "sk-ant-oat01-NEW"


def _isolated_account(now: datetime) -> Account:
    """A fresh OAuth account (obtained_at == now → no network refresh fires)."""
    return Account(
        name="matri",
        runtime_token="sk-ant-oat01-FRESH" + "a" * 90,
        label="matri",
        created_at=now - timedelta(days=1),
        plan="max_20x",
        refresh_token="sk-ant-ort01-SECRET" + "b" * 90,
        runtime_token_obtained_at=now,
        refresh_token_obtained_at=now,
    )


def test_isolated_sync_never_touches_global_credentials(tmp_path, monkeypatch) -> None:
    """Isolation mode must NEVER create or rewrite ~/.claude/.credentials.json.

    The former global mirror silently switched RUNNING headless sessions
    (which re-read the file every turn) to whichever account was launched
    last, invalidating their org-scoped prompt cache mid-run. Headless
    consumers pin an account via CLAUDE_CONFIG_DIR=<configs>/<account>;
    the default config dir is out of bounds for the rotator in this mode.
    """
    home = _fake_home(tmp_path, monkeypatch)
    p = _paths(tmp_path)
    save_config(p, RotateConfig(session_isolation=True))

    now = datetime.now(UTC)
    Store(p).save({"matri": _isolated_account(now)})
    write_current_session(p, CurrentSession(account_name="matri"))

    global_file = home / ".claude" / ".credentials.json"

    # Absent → the tick must not create it.
    assert execute(p) == 0
    assert not global_file.exists(), "isolation mode created the global file"

    # Present with foreign content (a manual non-rotate login) → untouched.
    global_file.write_text('{"claudeAiOauth": {"accessToken": "manual-login"}}')
    mtime_before = global_file.stat().st_mtime_ns
    assert execute(p) == 0
    assert global_file.stat().st_mtime_ns == mtime_before
    assert "manual-login" in global_file.read_text()
