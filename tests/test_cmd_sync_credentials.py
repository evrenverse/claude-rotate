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


def test_isolated_sync_mirrors_session_creds_to_global_fallback(tmp_path, monkeypatch) -> None:
    """Isolation mode keeps ~/.claude/.credentials.json fresh for headless
    consumers (CI scripts, the enniflow worker) that never set CLAUDE_CONFIG_DIR
    and therefore read the default config dir. The mirror carries full session
    scopes but strips the refresh token, so a headless `claude` can never
    double-spend the family's refresh token (nothing reconciles the global file
    back in isolation mode).
    """
    _fake_home(tmp_path, monkeypatch)
    p = _paths(tmp_path)
    save_config(p, RotateConfig(session_isolation=True))

    now = datetime.now(UTC)
    acct = _isolated_account(now)
    Store(p).save({"matri": acct})
    write_current_session(p, CurrentSession(account_name="matri"))

    # The global fallback starts empty (this is exactly the stale-token bug).
    assert CredentialsFile().read() is None

    assert execute(p) == 0

    mirrored = CredentialsFile().read()
    assert mirrored is not None, "isolation mode must keep ~/.claude fresh"
    assert mirrored.access_token == acct.runtime_token
    assert mirrored.refresh_token is None, "refresh token must be stripped"
    assert "user:sessions:claude_code" in mirrored.scopes, "full session scope required"
    # The real token at rest is never weakened — accounts.json keeps its refresh token.
    assert Store(p).load()["matri"].refresh_token == acct.refresh_token


def test_isolated_mirror_skips_rewrite_when_global_already_current(tmp_path, monkeypatch) -> None:
    """A re-mirror with an unchanged token must not rewrite the file, so the
    2-minute cron does not churn the global credentials file on every tick.
    """
    home = _fake_home(tmp_path, monkeypatch)
    p = _paths(tmp_path)
    save_config(p, RotateConfig(session_isolation=True))

    now = datetime.now(UTC)
    Store(p).save({"matri": _isolated_account(now)})
    write_current_session(p, CurrentSession(account_name="matri"))

    global_file = home / ".claude" / ".credentials.json"
    assert execute(p) == 0  # first tick → writes the global fallback
    mtime_after_first = global_file.stat().st_mtime_ns

    assert execute(p) == 0  # second tick → token unchanged, must be a no-op
    mtime_after_second = global_file.stat().st_mtime_ns
    assert mtime_after_second == mtime_after_first
