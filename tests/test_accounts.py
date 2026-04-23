from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_rotate.accounts import (
    SCHEMA_VERSION,
    Account,
    Store,
    account_from_dict,
    resolve_name,
)
from claude_rotate.config import Paths
from claude_rotate.errors import ConfigError

FIXTURE = Path(__file__).parent / "fixtures" / "accounts_v7.json"
FIXTURE_V6 = Path(__file__).parent / "fixtures" / "accounts_v6.json"


def test_schema_version() -> None:
    assert SCHEMA_VERSION == 8


def test_v7_schema_still_loadable(tmp_path: Path) -> None:
    """v7 accounts.json must still load — obtained_at fields default to None."""
    paths = Paths(config_dir=tmp_path / "c", cache_dir=tmp_path / "x", state_dir=tmp_path / "s")
    paths.config_dir.mkdir(parents=True)
    paths.accounts_file.write_text(FIXTURE.read_text())  # v7 fixture
    loaded = Store(paths).load()
    for acct in loaded.values():
        assert acct.runtime_token_obtained_at is None
        assert acct.refresh_token_obtained_at is None


def test_v8_roundtrip(tmp_path: Path) -> None:
    """v8 obtained_at fields serialise and deserialise correctly."""
    paths = Paths(config_dir=tmp_path / "c", cache_dir=tmp_path / "x", state_dir=tmp_path / "s")
    paths.config_dir.mkdir(parents=True)
    now = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
    acct = Account(
        name="test",
        runtime_token="sk-ant-oat01-" + "a" * 100,
        label="Test",
        created_at=now,
        runtime_token_obtained_at=now,
        refresh_token_obtained_at=now,
        refresh_token="sk-ant-ort01-" + "b" * 100,
    )
    Store(paths).save({"test": acct})
    loaded = Store(paths).load()["test"]
    assert loaded.runtime_token_obtained_at == now
    assert loaded.refresh_token_obtained_at == now


def test_v6_schema_still_loadable(tmp_path: Path) -> None:
    """v6 accounts.json (pre-manual-expiry) must still load cleanly."""
    paths = Paths(config_dir=tmp_path / "c", cache_dir=tmp_path / "x", state_dir=tmp_path / "s")
    paths.config_dir.mkdir(parents=True)
    paths.accounts_file.write_text(FIXTURE_V6.read_text())
    loaded = Store(paths).load()
    assert set(loaded.keys()) == {"main", "backup"}
    # field absent in v6 → default None
    assert loaded["main"].subscription_expires_at_manual is None
    assert loaded["backup"].subscription_expires_at_manual is None


def test_manual_expiry_override_precedence() -> None:
    """effective_expires_at prefers the manual override when set."""
    raw = json.loads(FIXTURE.read_text())["accounts"]["backup"]
    a = account_from_dict("backup", raw)
    assert a.subscription_expires_at is not None
    assert a.subscription_expires_at_manual is not None
    assert a.subscription_expires_at.day == 30  # 2026-05-30
    assert a.subscription_expires_at_manual.day == 24  # 2026-04-24
    # manual wins
    assert a.effective_expires_at == a.subscription_expires_at_manual


def test_account_from_dict_full() -> None:
    raw = json.loads(FIXTURE.read_text())["accounts"]["main"]
    a = account_from_dict("main", raw)
    assert a.name == "main"
    assert a.runtime_token.startswith("sk-ant-oat01-")
    assert a.label == "Max-20 main"
    assert a.plan == "max_20x"
    assert a.email == "main@example.com"
    assert a.subscription_expires_at is None
    assert a.pinned is False
    assert a.refresh_token == "sk-ant-ort01-mainrefreshtoken"


def test_account_from_dict_null_refresh_token() -> None:
    raw = json.loads(FIXTURE.read_text())["accounts"]["backup"]
    a = account_from_dict("backup", raw)
    assert a.refresh_token is None


def test_account_from_dict_subscription_status() -> None:
    raw = json.loads(FIXTURE.read_text())["accounts"]["main"]
    a = account_from_dict("main", raw)
    assert a.subscription_status == "active"


def test_account_from_dict_null_subscription_status() -> None:
    raw = json.loads(FIXTURE.read_text())["accounts"]["backup"]
    a = account_from_dict("backup", raw)
    assert a.subscription_status is None


def test_account_from_dict_with_subscription_expiry() -> None:
    raw = json.loads(FIXTURE.read_text())["accounts"]["backup"]
    a = account_from_dict("backup", raw)
    assert a.subscription_expires_at is not None
    assert a.subscription_expires_at.year == 2026


def test_account_to_dict_roundtrip() -> None:
    raw = json.loads(FIXTURE.read_text())["accounts"]["main"]
    a = account_from_dict("main", raw)
    # v8 adds two obtained_at keys absent from the v7 fixture; default None.
    expected = {
        **raw,
        "runtime_token_obtained_at": None,
        "refresh_token_obtained_at": None,
    }
    assert a.to_dict() == expected


def test_account_name_is_not_serialized_into_dict() -> None:
    raw = json.loads(FIXTURE.read_text())["accounts"]["main"]
    a = account_from_dict("main", raw)
    d = a.to_dict()
    assert "name" not in d


# ---------------------------------------------------------------------------
# Task 8 - Store tests
# ---------------------------------------------------------------------------


def make_paths(root: Path) -> Paths:
    return Paths(
        config_dir=root / "config",
        cache_dir=root / "cache",
        state_dir=root / "state",
    )


def test_store_load_missing_returns_empty(tmp_path: Path) -> None:
    store = Store(make_paths(tmp_path))
    accounts = store.load()
    assert accounts == {}


def test_store_save_and_reload(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    store = Store(paths)
    a = Account(
        name="main",
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label="Max-20 main",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        plan="max_20x",
    )
    store.save({"main": a})
    reloaded = store.load()
    assert set(reloaded.keys()) == {"main"}
    assert reloaded["main"].runtime_token == a.runtime_token
    assert paths.accounts_file.stat().st_mode & 0o777 == 0o600


def test_store_save_writes_atomically(tmp_path: Path) -> None:
    """No temp file should remain after a successful write."""
    paths = make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    store = Store(paths)
    a = Account(
        name="main",
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label="lbl",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        plan="max_20x",
    )
    store.save({"main": a})
    leftover = list(paths.config_dir.glob("accounts.json.*"))
    # Only the .lock file is allowed; no temp artifacts
    assert [p.name for p in leftover] == ["accounts.json.lock"] or leftover == []


def test_store_rejects_unknown_schema_version(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.accounts_file.write_text('{"version": 99, "accounts": {}}')
    store = Store(paths)
    with pytest.raises(ConfigError, match="schema version"):
        store.load()


def _make_accounts(
    *pairs: tuple[str, str | None],
) -> dict[str, Account]:
    out = {}
    for name, email in pairs:
        out[name] = Account(
            name=name,
            runtime_token="sk-ant-oat01-" + "a" * 96,
            label=name,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            plan="max_20x",
            email=email,
        )
    return out


def test_resolve_name_by_handle() -> None:
    accts = _make_accounts(("work", "work@example.com"), ("personal", "personal@example.com"))
    assert resolve_name(accts, "work") == "work"


def test_resolve_name_by_email() -> None:
    accts = _make_accounts(("work", "work@example.com"), ("personal", "personal@example.com"))
    assert resolve_name(accts, "personal@example.com") == "personal"


def test_resolve_name_by_email_case_insensitive() -> None:
    accts = _make_accounts(("personal", "PERSONAL@Example.COM"))
    assert resolve_name(accts, "personal@example.com") == "personal"


def test_resolve_name_missing_returns_none() -> None:
    accts = _make_accounts(("work", "work@example.com"))
    assert resolve_name(accts, "nobody") is None
    assert resolve_name(accts, "unknown@example.com") is None


def test_resolve_name_ambiguous_email_raises() -> None:
    """Two accounts with the same email — reject and force handle usage."""
    accts = _make_accounts(("a", "dup@example.com"), ("b", "dup@example.com"))
    with pytest.raises(ConfigError, match="multiple accounts"):
        resolve_name(accts, "dup@example.com")


def test_resolve_name_handle_takes_precedence_over_email_match() -> None:
    """If 'foo' is both a handle and an email local-part, handle wins."""
    accts = _make_accounts(("foo", "foo@example.com"))
    # 'foo' is the handle — match directly, don't bother with email lookup
    assert resolve_name(accts, "foo") == "foo"


def test_store_corrupt_file_moves_to_backup_and_raises(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.accounts_file.write_text("not json at all")
    store = Store(paths)
    with pytest.raises(ConfigError, match="malformed"):
        store.load()
    # backup created
    backups = list(paths.config_dir.glob("accounts.json.corrupt-*"))
    assert len(backups) == 1
