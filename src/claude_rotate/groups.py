"""Account groups bound to directories — pure model, directory resolution, allowed set.

A group lists accounts and (optionally) directories. A group with at least one
directory is *bound*: its accounts are allowed only in sessions inside those
directories, and every other directory excludes them. An unbound group
reserves nothing. "Allowed here" is phrased so it stays correct if an account
ever sits in several groups: member of the group matching the directory, or
(no group matches) member of no bound group.

No I/O besides the ``.git`` / ``commondir`` reads ``session_path`` needs.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from claude_rotate.accounts import validate_account_name
from claude_rotate.errors import AccountError


@dataclass(frozen=True)
class Group:
    name: str
    accounts: tuple[str, ...] = ()
    dirs: tuple[str, ...] = ()


def _strings(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(dict.fromkeys(x for x in raw if isinstance(x, str)))


def parse_groups(raw: object) -> dict[str, Group]:
    """Tolerant reader: anything malformed is dropped, never raised."""
    if not isinstance(raw, dict):
        return {}
    groups: dict[str, Group] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        try:
            validate_account_name(name)
        except AccountError:
            continue
        groups[name] = Group(
            name=name, accounts=_strings(entry.get("accounts")), dirs=_strings(entry.get("dirs"))
        )
    return groups


def dump_groups(groups: Mapping[str, Group]) -> dict[str, dict[str, list[str]]]:
    return {
        name: {"accounts": list(g.accounts), "dirs": list(g.dirs)}
        for name, g in sorted(groups.items())
    }


def _linked_worktree_main(git_file: Path, root: Path, p: Path) -> Path | None:
    """Map ``p`` (inside linked worktree ``root``) to the main worktree, or None."""
    text = git_file.read_text().strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = (root / text.removeprefix("gitdir:").strip()).resolve()
    commondir = gitdir / "commondir"
    if not commondir.is_file():
        return None  # submodule: no commondir, nothing to map
    common = (gitdir / commondir.read_text().strip()).resolve()
    if common.name != ".git":
        return None  # bare or separate-git-dir repo: no main worktree to map to
    return common.parent / p.relative_to(root)


def session_path(path: str | Path) -> Path:
    """Resolve ``path`` for group matching; never raises.

    ``expanduser`` + ``realpath``, then a path inside a linked git worktree maps
    to the same relative path in the repo's main worktree, so a worktree outside
    the repo (``~/.paseo/worktrees/…``) still lands in the repo's group. Any
    failure (no git, broken repo, unreadable file) yields the plain resolved path;
    a path that cannot be resolved at all (embedded NUL, deleted cwd) comes back
    as given, which no stored (absolute) directory can match.
    """
    try:
        p = Path(os.path.realpath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return Path(path)
    for root in (p, *p.parents):
        git = root / ".git"
        try:
            if git.is_dir():
                return p  # main worktree or plain repo
            if git.is_file():
                return _linked_worktree_main(git, root, p) or p
        except (OSError, ValueError):
            return p
    return p


def matching_group(groups: Mapping[str, Group], path: Path) -> Group | None:
    """Group whose longest bound directory equals ``path`` or is a parent of it."""
    best: Group | None = None
    best_len = -1
    for _, g in sorted(groups.items()):  # sorted: a same-dir tie resolves deterministically
        for d in g.dirs:
            dp = Path(d)
            if (dp == path or dp in path.parents) and len(dp.parts) > best_len:
                best, best_len = g, len(dp.parts)
    return best


def has_bound_groups(groups: Mapping[str, Group]) -> bool:
    return any(g.dirs for g in groups.values())


def allowed_accounts(
    groups: Mapping[str, Group], names: Iterable[str], path: Path
) -> frozenset[str]:
    group = matching_group(groups, path)
    if group is not None:
        return frozenset(n for n in names if n in group.accounts)
    reserved = {a for g in groups.values() if g.dirs for a in g.accounts}
    return frozenset(n for n in names if n not in reserved)


def group_of(groups: Mapping[str, Group], account: str) -> str | None:
    return next((name for name, g in sorted(groups.items()) if account in g.accounts), None)


def with_member_renamed(groups: Mapping[str, Group], old: str, new: str) -> dict[str, Group]:
    return {
        name: replace(g, accounts=tuple(dict.fromkeys(new if a == old else a for a in g.accounts)))
        for name, g in groups.items()
    }


def without_member(groups: Mapping[str, Group], account: str) -> dict[str, Group]:
    return {
        name: replace(g, accounts=tuple(a for a in g.accounts if a != account))
        for name, g in groups.items()
    }
