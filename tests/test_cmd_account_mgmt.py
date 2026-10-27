# tests/test_cmd_account_mgmt.py
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths
from claude_rotate.groups import Group
from claude_rotate.settings import RotateConfig, load_config, save_config


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def _acc(name: str = "main") -> Account:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    return Account(
        name=name,
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label=name,
        created_at=now,
        plan="max_20x",
        email=f"{name}@example.com",
    )


def test_list_prints_each_account(tmp_path, capsys) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})
    from claude_rotate.commands import list_cmd

    list_cmd.execute(p)
    out = capsys.readouterr().err
    assert "a" in out and "b" in out


def test_remove_requires_confirmation(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a")})
    from claude_rotate.commands import remove

    monkeypatch.setattr("builtins.input", lambda _: "n")
    rc = remove.execute(p, SimpleNamespace(name="a", yes=False))
    assert rc == 1
    assert "a" in Store(p).load()


def test_remove_with_yes_skips_prompt(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a")})
    from claude_rotate.commands import remove

    rc = remove.execute(p, SimpleNamespace(name="a", yes=True))
    assert rc == 0
    assert "a" not in Store(p).load()


def test_remove_nonexistent_account_errors(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    from claude_rotate.commands import remove

    rc = remove.execute(p, SimpleNamespace(name="ghost", yes=True))
    assert rc == 1


def test_rename_moves_key(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a")})
    from claude_rotate.commands import rename

    rc = rename.execute(p, SimpleNamespace(old="a", new="b"))
    assert rc == 0
    accounts = Store(p).load()
    assert "b" in accounts and "a" not in accounts
    assert accounts["b"].name == "b"


def test_rename_to_existing_name_fails(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})
    from claude_rotate.commands import rename

    rc = rename.execute(p, SimpleNamespace(old="a", new="b"))
    assert rc == 1


def test_login_delegates_to_interactive(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    from claude_rotate.commands import login as login_cmd

    with patch("claude_rotate.commands.login.do_login_interactive") as mock:
        mock.return_value = _acc("x")
        rc = login_cmd.execute(
            p,
            SimpleNamespace(
                email="x@example.com",
                name="x",
                replace=False,
                from_env=False,
                token_file=None,
                plan="max_20x",
            ),
        )
    assert rc == 0
    mock.assert_called_once()


def test_login_from_env_delegates_to_do_login_from_env(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    from claude_rotate.commands import login as login_cmd

    with patch("claude_rotate.commands.login.do_login_from_env") as mock:
        mock.return_value = _acc("x")
        rc = login_cmd.execute(
            p,
            SimpleNamespace(
                email="x@example.com",
                name="x",
                replace=False,
                from_env=True,
                token_file=None,
                plan="max_20x",
            ),
        )
    assert rc == 0
    mock.assert_called_once()


def _group_config(p: Paths, **groups: Group) -> None:
    save_config(p, RotateConfig(session_isolation=True, groups=groups))


def test_rename_updates_group_membership(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_ROTATE_SESSION_ISOLATION", raising=False)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})
    _group_config(p, g=Group("g", ("a", "b"), ("/x",)))
    from claude_rotate.commands import rename

    assert rename.execute(p, SimpleNamespace(old="a", new="z")) == 0
    cfg = load_config(p, apply_env=False)
    assert cfg.groups["g"] == Group("g", ("z", "b"), ("/x",))
    assert cfg.session_isolation is True


def test_remove_drops_group_membership(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_ROTATE_SESSION_ISOLATION", raising=False)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a")})
    _group_config(p, g=Group("g", ("a",), ("/x",)))
    from claude_rotate.commands import remove

    assert remove.execute(p, SimpleNamespace(name="a", yes=True)) == 0
    assert load_config(p, apply_env=False).groups["g"] == Group("g", (), ("/x",))


def test_rename_and_remove_leave_config_alone_without_groups(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})
    from claude_rotate.commands import remove, rename

    assert rename.execute(p, SimpleNamespace(old="a", new="z")) == 0
    assert remove.execute(p, SimpleNamespace(name="b", yes=True)) == 0
    assert not p.config_file.exists()
