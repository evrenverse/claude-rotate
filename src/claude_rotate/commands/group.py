"""`claude-rotate group …` — account groups and their directory bindings."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from claude_rotate.accounts import Store, resolve_name, validate_account_name
from claude_rotate.config import Paths
from claude_rotate.errors import AccountError
from claude_rotate.groups import Group, session_path
from claude_rotate.settings import load_config, save_config

_NO_ISOLATION = (
    "binding a group needs session isolation, which is off. "
    "Enable it with: claude-rotate config set session_isolation true "
    "(a CLAUDE_ROTATE_SESSION_ISOLATION override in the environment wins over the config)"
)


def _say(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def _tilde(path: str) -> str:
    try:
        return str("~" / Path(path).relative_to(Path.home()))
    except ValueError:
        return path


def _list(groups: dict[str, Group]) -> int:
    if not groups:
        _say("\n  No groups. Create one:\n")
        _say("    claude-rotate group create work")
        _say("    claude-rotate group add work <account>...")
        _say("    claude-rotate group bind work ~/projects/work\n")
        return 0
    _say()
    for name, g in sorted(groups.items()):
        _say(f"  {name}")
        _say(f"    accounts  {', '.join(g.accounts) or '-'}")
        _say(f"    dirs      {', '.join(_tilde(d) for d in g.dirs) or '-'}")
    _say()
    return 0


def _create(groups: dict[str, Group], name: str) -> str | None:
    try:
        validate_account_name(name)
    except AccountError as exc:
        return str(exc)
    if name == "ungrouped":  # the dashboard's section title for group-less accounts
        return f"{name!r} is reserved: the dashboard lists accounts without a group under it"
    if name in groups:
        return f"group {name!r} already exists"
    groups[name] = Group(name)
    return None


def _add(paths: Paths, groups: dict[str, Group], name: str, given: list[str]) -> str | None:
    store = Store(paths).load()
    resolved: list[str] = []
    for ident in given:  # resolve everything first so a typo never half-applies
        account = resolve_name(store, ident)
        if account is None:
            return f"account {ident!r} not found"
        resolved.append(account)
    for account in dict.fromkeys(resolved):
        for other in [g for g in groups.values() if g.name != name and account in g.accounts]:
            groups[other.name] = replace(
                other, accounts=tuple(a for a in other.accounts if a != account)
            )
            _say(f"  {account}: moved from {other.name}")
        g = groups[name]
        if account not in g.accounts:
            groups[name] = replace(g, accounts=(*g.accounts, account))
    return None


def _remove(paths: Paths, groups: dict[str, Group], name: str, given: list[str]) -> str | None:
    store = Store(paths).load()
    g = groups[name]
    # Fall back to the literal so a stale member (account gone, hand-edited file) can still go.
    drop = {resolve_name(store, ident) or ident for ident in given}
    missing = sorted(drop - set(g.accounts))
    if missing:
        return f"{', '.join(map(repr, missing))} not in group {name!r}"
    groups[name] = replace(g, accounts=tuple(a for a in g.accounts if a not in drop))
    return None


def _bind(paths: Paths, groups: dict[str, Group], name: str, raw: str | None) -> str | None:
    if not load_config(paths).session_isolation:  # env applied: that is what `run` will see
        return _NO_ISOLATION
    given = Path(raw or os.getcwd()).expanduser()
    if not given.is_dir():
        return f"{str(given)!r} is not an existing directory"
    target = str(session_path(given))
    for other in groups.values():
        if other.name != name and target in other.dirs:
            return f"{_tilde(target)} is already bound to group {other.name!r}"
    g = groups[name]
    if target not in g.dirs:
        groups[name] = replace(g, dirs=(*g.dirs, target))
    _say(f"  ✓ {name} → {_tilde(target)}")
    return None


def _unbind(groups: dict[str, Group], name: str, raw: str | None) -> str | None:
    given = raw or os.getcwd()
    # The literal string also matches, so a directory that no longer exists can be unbound.
    wanted = {given, str(session_path(given))}
    g = groups[name]
    kept = tuple(d for d in g.dirs if d not in wanted)
    if kept == g.dirs:
        return f"{given!r} is not bound to group {name!r}"
    groups[name] = replace(g, dirs=kept)
    return None


def execute(paths: Paths, args: argparse.Namespace) -> int:
    action = args.group_action or "list"
    # Never the env-applied value: this config gets written back, and a transient
    # CLAUDE_ROTATE_SESSION_ISOLATION must not become the persisted setting.
    cfg = load_config(paths, apply_env=False)
    groups = dict(cfg.groups)
    if action == "list":
        return _list(groups)

    # shortcut: config.json writes are not locked; two group commands at the same instant can
    # lose one update — add a file_lock if this ever runs unattended.
    name: str = args.group
    err: str | None
    if action == "create":
        err = _create(groups, name)
    elif name not in groups:
        err = f"group {name!r} not found"
    elif action == "delete":
        del groups[name]  # members simply become ungrouped
        err = None
    elif action == "add":
        err = _add(paths, groups, name, args.accounts)
    elif action == "remove":
        err = _remove(paths, groups, name, args.accounts)
    elif action == "bind":
        err = _bind(paths, groups, name, args.dir)
    elif action == "unbind":
        err = _unbind(groups, name, args.dir)
    else:
        err = f"unknown group action {action!r}"
    if err is not None:
        _say(f"error: {err}")
        return 1

    save_config(paths, replace(cfg, groups=groups))
    if action != "bind":  # bind already said where it bound
        _say(f"  ✓ {action} {name}")
    return 0
