"""Tests for disable and enable commands."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def _acc(name: str = "main", **kw) -> Account:  # type: ignore[no-untyped-def]
    now = datetime(2026, 4, 22, tzinfo=UTC)
    defaults = {
        "name": name,
        "runtime_token": "sk-ant-oat01-" + "a" * 96,
        "refresh_token": "sk-ant-ort01-" + "r" * 96,
        "label": f"Max-20 {name}",
        "created_at": now,
        "plan": "max_20x",
        "email": f"{name}@example.com",
        "metadata_refreshed_at": now,
    }
    defaults.update(kw)
    return Account(**defaults)  # type: ignore[arg-type]


def test_disable_sets_only_the_named_account(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})
    from claude_rotate.commands import disable

    rc = disable.execute(p, "b", disabled=True)
    assert rc == 0
    accounts = Store(p).load()
    assert accounts["a"].disabled is False
    assert accounts["b"].disabled is True


def test_enable_clears_the_flag(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": replace(_acc("b"), disabled=True)})
    from claude_rotate.commands import disable

    rc = disable.execute(p, "b", disabled=False)
    assert rc == 0
    assert Store(p).load()["b"].disabled is False


def test_disable_supports_multiple_accounts(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})
    from claude_rotate.commands import disable

    disable.execute(p, "a", disabled=True)
    disable.execute(p, "b", disabled=True)
    accounts = Store(p).load()
    assert accounts["a"].disabled is True
    assert accounts["b"].disabled is True


def test_disable_by_email(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a")})
    from claude_rotate.commands import disable

    rc = disable.execute(p, "a@example.com", disabled=True)
    assert rc == 0
    assert Store(p).load()["a"].disabled is True


def test_disable_unknown_account_errors(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a")})
    from claude_rotate.commands import disable

    rc = disable.execute(p, "ghost", disabled=True)
    assert rc == 1
    assert Store(p).load()["a"].disabled is False


def test_disabling_a_pinned_account_clears_the_pin(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": replace(_acc("a"), pinned=True)})
    from claude_rotate.commands import disable

    disable.execute(p, "a", disabled=True)
    loaded = Store(p).load()["a"]
    assert loaded.disabled is True
    assert loaded.pinned is False


def test_pinning_a_disabled_account_clears_the_disable(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": replace(_acc("a"), disabled=True), "b": _acc("b")})
    from claude_rotate.commands import pin

    pin.execute(p, name="a", pinned=True)
    loaded = Store(p).load()["a"]
    assert loaded.pinned is True
    assert loaded.disabled is False
