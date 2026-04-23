"""Tests for pin and unpin commands."""

from __future__ import annotations

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


def test_pin_sets_single_pinned_account(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})
    from claude_rotate.commands import pin

    pin.execute(p, name="b", pinned=True)
    accounts = Store(p).load()
    assert accounts["a"].pinned is False
    assert accounts["b"].pinned is True


def test_unpin_clears_all_pins(tmp_path: Path) -> None:
    from dataclasses import replace

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": replace(_acc("b"), pinned=True)})
    from claude_rotate.commands import pin

    pin.execute(p, name=None, pinned=False)
    accounts = Store(p).load()
    assert all(a.pinned is False for a in accounts.values())
