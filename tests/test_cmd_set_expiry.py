"""Tests for `claude-rotate set-expiry`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_rotate.accounts import Account, Store
from claude_rotate.commands import set_expiry
from claude_rotate.config import Paths
from claude_rotate.errors import AccountError


def _paths(root: Path) -> Paths:
    return Paths(
        config_dir=root / "config",
        cache_dir=root / "cache",
        state_dir=root / "state",
    )


def _seed(paths: Paths, name: str = "work") -> None:
    paths.config_dir.mkdir(parents=True)
    Store(paths).save(
        {
            name: Account(
                name=name,
                runtime_token="sk-ant-oat01-" + "a" * 96,
                label=f"Max-20 {name}",
                created_at=datetime(2026, 4, 1, tzinfo=UTC),
                plan="max_20x",
            )
        }
    )


def test_set_expiry_absolute_date(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    rc = set_expiry.execute(paths, "work", "2026-04-24")
    assert rc == 0
    acct = Store(paths).load()["work"]
    assert acct.subscription_expires_at_manual is not None
    assert acct.subscription_expires_at_manual.date().isoformat() == "2026-04-24"
    assert acct.effective_expires_at == acct.subscription_expires_at_manual


def test_set_expiry_relative_days(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    rc = set_expiry.execute(paths, "work", "5d")
    assert rc == 0
    acct = Store(paths).load()["work"]
    assert acct.subscription_expires_at_manual is not None
    delta = acct.subscription_expires_at_manual - datetime.now(UTC)
    assert 4 <= delta.days <= 5


def test_set_expiry_empty_clears(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    set_expiry.execute(paths, "work", "2026-04-24")
    rc = set_expiry.execute(paths, "work", "")
    assert rc == 0
    assert Store(paths).load()["work"].subscription_expires_at_manual is None


def test_set_expiry_unknown_account(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    _seed(paths, name="other")
    rc = set_expiry.execute(paths, "work", "5d")
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_set_expiry_invalid_format(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    with pytest.raises(AccountError, match="invalid expiry"):
        set_expiry.execute(paths, "work", "tomorrow")
