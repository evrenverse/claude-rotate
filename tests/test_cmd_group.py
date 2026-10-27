"""Tests for `claude-rotate group …`."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_rotate.accounts import Account, Store
from claude_rotate.cli import main
from claude_rotate.commands import group as group_cmd
from claude_rotate.config import Paths, paths
from claude_rotate.groups import Group
from claude_rotate.settings import RotateConfig, load_config, save_config


def _acc(name: str) -> Account:
    return Account(
        name=name,
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label=name,
        created_at=datetime(2026, 4, 22, tzinfo=UTC),
        plan="max_20x",
        email=f"{name}@example.com",
    )


def _ns(action: str | None, group: str | None = None, *accounts: str, dir: str | None = None):  # type: ignore[no-untyped-def]
    return argparse.Namespace(group_action=action, group=group, accounts=list(accounts), dir=dir)


@pytest.fixture
def p(rotate_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    monkeypatch.delenv("CLAUDE_ROTATE_SESSION_ISOLATION", raising=False)
    p = paths()
    Store(p).save({n: _acc(n) for n in ("a", "b", "c")})
    save_config(p, RotateConfig(session_isolation=True))
    return p


def _groups(p: Paths) -> dict[str, Group]:
    return dict(load_config(p, apply_env=False).groups)


def test_create_add_bind_list(p: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    k = tmp_path / "k"
    k.mkdir()
    assert group_cmd.execute(p, _ns("create", "kunde")) == 0
    assert group_cmd.execute(p, _ns("add", "kunde", "a", "b")) == 0
    assert group_cmd.execute(p, _ns("bind", "kunde", dir=str(k))) == 0
    assert _groups(p)["kunde"] == Group("kunde", ("a", "b"), (str(k.resolve()),))

    capsys.readouterr()
    assert group_cmd.execute(p, _ns("list")) == 0
    out = capsys.readouterr().err
    assert "kunde" in out and "a, b" in out and str(k.resolve()) in out


def test_list_shortens_home_to_tilde(
    p: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = Path.home()
    save_config(
        p,
        RotateConfig(
            session_isolation=True, groups={"g": Group("g", (), (str(home / "x" / "y"),))}
        ),
    )
    assert group_cmd.execute(p, _ns(None)) == 0  # no action behaves like list
    out = capsys.readouterr().err
    assert "~/x/y" in out and str(home) not in out


def test_list_without_groups_shows_example(p: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    assert group_cmd.execute(p, _ns("list")) == 0
    out = capsys.readouterr().err
    assert "No groups." in out and "group create" in out


def test_create_existing_group_fails(p: Paths) -> None:
    assert group_cmd.execute(p, _ns("create", "g")) == 0
    assert group_cmd.execute(p, _ns("create", "g")) == 1


def test_create_rejects_the_reserved_name_ungrouped(
    p: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dashboard titles the section of group-less accounts ``ungrouped``."""
    assert group_cmd.execute(p, _ns("create", "ungrouped")) == 1
    assert "ungrouped" in capsys.readouterr().err
    assert "ungrouped" not in _groups(p)


def test_unknown_group_fails(p: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    for action in ("delete", "add", "remove", "bind", "unbind"):
        assert group_cmd.execute(p, _ns(action, "nope", "a")) == 1
    assert "error:" in capsys.readouterr().err


def test_add_moves_from_previous_group(p: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    group_cmd.execute(p, _ns("create", "g1"))
    group_cmd.execute(p, _ns("create", "g2"))
    group_cmd.execute(p, _ns("add", "g1", "a"))
    capsys.readouterr()
    assert group_cmd.execute(p, _ns("add", "g2", "a")) == 0
    assert "moved from g1" in capsys.readouterr().err
    groups = _groups(p)
    assert groups["g1"].accounts == ()
    assert groups["g2"].accounts == ("a",)


def test_add_resolves_email_and_is_idempotent(p: Paths) -> None:
    group_cmd.execute(p, _ns("create", "g"))
    assert group_cmd.execute(p, _ns("add", "g", "a@example.com", "a", "b")) == 0
    assert _groups(p)["g"].accounts == ("a", "b")


def test_add_unknown_account_fails(p: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    group_cmd.execute(p, _ns("create", "g"))
    before = _groups(p)
    assert group_cmd.execute(p, _ns("add", "g", "a", "ghost")) == 1
    assert "ghost" in capsys.readouterr().err
    assert _groups(p) == before  # "a" was not half-added


def test_remove_member_ungroups_account(p: Paths) -> None:
    group_cmd.execute(p, _ns("create", "g"))
    group_cmd.execute(p, _ns("add", "g", "a", "b"))
    assert group_cmd.execute(p, _ns("remove", "g", "a")) == 0
    assert _groups(p)["g"].accounts == ("b",)
    assert group_cmd.execute(p, _ns("remove", "g", "c")) == 1  # not a member


def test_bind_refuses_without_isolation(
    p: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_config(p, RotateConfig(session_isolation=False, groups={"g": Group("g")}))
    assert group_cmd.execute(p, _ns("bind", "g", dir=str(tmp_path))) == 1
    assert "session_isolation true" in capsys.readouterr().err
    assert _groups(p)["g"].dirs == ()


def test_bind_refuses_when_env_turns_isolation_off(
    p: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group_cmd.execute(p, _ns("create", "g"))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION_ISOLATION", "0")
    assert group_cmd.execute(p, _ns("bind", "g", dir=str(tmp_path))) == 1


def test_bind_refuses_missing_dir(p: Paths, tmp_path: Path) -> None:
    group_cmd.execute(p, _ns("create", "g"))
    assert group_cmd.execute(p, _ns("bind", "g", dir=str(tmp_path / "missing"))) == 1
    assert _groups(p)["g"].dirs == ()


def test_bind_refuses_dir_of_other_group(
    p: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    group_cmd.execute(p, _ns("create", "g1"))
    group_cmd.execute(p, _ns("create", "g2"))
    assert group_cmd.execute(p, _ns("bind", "g1", dir=str(tmp_path))) == 0
    capsys.readouterr()
    assert group_cmd.execute(p, _ns("bind", "g2", dir=str(tmp_path))) == 1
    assert "g1" in capsys.readouterr().err
    assert _groups(p)["g2"].dirs == ()


def test_bind_resolves_symlink_and_is_idempotent(p: Paths, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    group_cmd.execute(p, _ns("create", "g"))
    assert group_cmd.execute(p, _ns("bind", "g", dir=str(link))) == 0
    assert group_cmd.execute(p, _ns("bind", "g", dir=str(real))) == 0
    assert _groups(p)["g"].dirs == (str(real.resolve()),)


def test_bind_defaults_to_cwd(p: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "proj"
    d.mkdir()
    monkeypatch.chdir(d)
    group_cmd.execute(p, _ns("create", "g"))
    assert group_cmd.execute(p, _ns("bind", "g")) == 0
    assert _groups(p)["g"].dirs == (str(d.resolve()),)
    assert group_cmd.execute(p, _ns("unbind", "g")) == 0
    assert _groups(p)["g"].dirs == ()


def test_unbind_deleted_dir_by_literal_path(p: Paths, tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    group_cmd.execute(p, _ns("create", "g"))
    group_cmd.execute(p, _ns("bind", "g", dir=str(gone)))
    gone.rmdir()
    assert group_cmd.execute(p, _ns("unbind", "g", dir=str(gone))) == 0
    assert _groups(p)["g"].dirs == ()
    assert group_cmd.execute(p, _ns("unbind", "g", dir=str(gone))) == 1  # nothing left to unbind


def test_unbind_and_delete(p: Paths, tmp_path: Path) -> None:
    group_cmd.execute(p, _ns("create", "g"))
    group_cmd.execute(p, _ns("add", "g", "a"))
    group_cmd.execute(p, _ns("bind", "g", dir=str(tmp_path)))
    assert group_cmd.execute(p, _ns("unbind", "g", dir=str(tmp_path))) == 0
    assert _groups(p)["g"] == Group("g", ("a",), ())
    assert group_cmd.execute(p, _ns("delete", "g")) == 0
    assert _groups(p) == {}
    assert "a" in Store(p).load()  # the account itself stays


def test_group_name_validated(p: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    assert group_cmd.execute(p, _ns("create", "../x")) == 1
    assert "error:" in capsys.readouterr().err
    assert _groups(p) == {}
    assert main(["group", "create", "../x"]) != 0


def test_group_commands_never_persist_isolation_env(
    p: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_config(p, RotateConfig(session_isolation=False))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION_ISOLATION", "1")
    assert group_cmd.execute(p, _ns("create", "g")) == 0
    assert json.loads(p.config_file.read_text())["session_isolation"] is False


def test_group_commands_keep_other_settings(p: Paths) -> None:
    save_config(
        p, RotateConfig(session_isolation=True, auto_resume_enabled=True, session_tracking=False)
    )
    group_cmd.execute(p, _ns("create", "g"))
    cfg = load_config(p, apply_env=False)
    assert cfg.auto_resume_enabled and not cfg.session_tracking and cfg.session_isolation


def test_cli_group_is_a_rotator_command(p: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["group", "list"]) == 0
    assert main(["group"]) == 0
    assert main(["group", "create", "g"]) == 0
    assert main(["group", "add", "g", "a", "b"]) == 0
    assert main(["group", "list"]) == 0
    assert "a, b" in capsys.readouterr().err
    assert main(["group", "bind", "g", str(p.config_dir)]) == 0
    assert _groups(p)["g"].dirs == (str(p.config_dir.resolve()),)
