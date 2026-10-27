from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from claude_rotate.groups import (
    Group,
    allowed_accounts,
    dump_groups,
    group_of,
    has_bound_groups,
    matching_group,
    parse_groups,
    session_path,
    with_member_renamed,
    without_member,
)


def _g(
    name: str, accounts: tuple[str, ...] | list[str] = (), dirs: tuple[Path, ...] | list[Path] = ()
) -> Group:
    return Group(name=name, accounts=tuple(accounts), dirs=tuple(str(d) for d in dirs))


def test_parse_dump_round_trip() -> None:
    raw = {"kunde": {"accounts": ["alpha01", "alpha02"], "dirs": ["/x/y"]}}
    assert dump_groups(parse_groups(raw)) == raw


def test_dump_groups_sorts_names() -> None:
    groups = {"b": _g("b"), "a": _g("a", ["x"])}
    assert list(dump_groups(groups)) == ["a", "b"]


def test_parse_groups_tolerates_garbage() -> None:
    assert parse_groups(None) == {}
    assert parse_groups([1]) == {}
    raw = {
        "../bad": {"accounts": ["a"]},
        "ok": {"accounts": [1, "a", "a"], "dirs": "x"},
        "notadict": ["a"],
        7: {"accounts": ["a"]},
    }
    assert parse_groups(raw) == {"ok": Group(name="ok", accounts=("a",), dirs=())}


def test_parse_groups_keeps_order_and_dedupes_dirs() -> None:
    raw = {"g": {"accounts": ["b", "a", "b"], "dirs": ["/y", "/x", "/y", 3]}}
    assert parse_groups(raw)["g"] == Group(name="g", accounts=("b", "a"), dirs=("/y", "/x"))


def test_matching_group_longest_prefix_wins(tmp_path: Path) -> None:
    outer, inner = tmp_path / "p", tmp_path / "p" / "repo"
    inner.mkdir(parents=True)
    groups = {"a": _g("a", ["x"], [outer]), "b": _g("b", ["y"], [inner])}
    match = matching_group(groups, inner / "src")
    assert match is not None and match.name == "b"
    match = matching_group(groups, outer / "other")
    assert match is not None and match.name == "a"
    match = matching_group(groups, inner)
    assert match is not None and match.name == "b"
    assert matching_group(groups, tmp_path) is None
    # prefix is by path component, not by string
    assert matching_group({"a": _g("a", ["x"], [tmp_path / "p"])}, tmp_path / "p2") is None


def test_has_bound_groups(tmp_path: Path) -> None:
    assert not has_bound_groups({})
    assert not has_bound_groups({"g": _g("g", ["a"])})
    assert has_bound_groups({"g": _g("g", ["a"], [tmp_path])})


def test_allowed_accounts_bound_dir_only_group_members(tmp_path: Path) -> None:
    groups = {"kunde": _g("kunde", ["alpha01", "alpha02"], [tmp_path])}
    names = ["alpha01", "alpha02", "free"]
    assert allowed_accounts(groups, names, tmp_path / "repo") == {"alpha01", "alpha02"}


def test_allowed_accounts_elsewhere_excludes_bound_members(tmp_path: Path) -> None:
    groups = {"kunde": _g("kunde", ["alpha01"], [tmp_path / "k"]), "loose": _g("loose", ["l1"])}
    assert allowed_accounts(groups, ["alpha01", "l1", "free"], tmp_path / "other") == {"l1", "free"}


def test_allowed_accounts_no_groups_allows_all(tmp_path: Path) -> None:
    assert allowed_accounts({}, ["a", "b"], tmp_path) == {"a", "b"}


def test_allowed_accounts_account_in_several_groups(tmp_path: Path) -> None:
    # future multi-group case: bound membership reserves the account elsewhere
    groups = {
        "bound": _g("bound", ["a", "b"], [tmp_path / "k"]),
        "loose": _g("loose", ["b", "c"]),
    }
    assert allowed_accounts(groups, ["a", "b", "c"], tmp_path / "other") == {"c"}
    assert allowed_accounts(groups, ["a", "b", "c"], tmp_path / "k") == {"a", "b"}


def test_matching_group_through_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    groups = {"g": _g("g", ["a"], [session_path(link)])}
    match = matching_group(groups, session_path(real / "."))
    assert match is not None and match.name == "g"
    match = matching_group(groups, session_path(link))
    assert match is not None and match.name == "g"


def _fake_linked_worktree(repo: Path, wt: Path) -> None:
    # No git needed: <repo>/.git/worktrees/wt plus a "gitdir:" file in the worktree.
    gitdir = repo / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (wt / "src").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}\n")


def test_session_path_maps_linked_worktree_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = tmp_path / "paseo" / "worktrees" / "wt"
    _fake_linked_worktree(repo, wt)
    assert session_path(wt / "src") == repo.resolve() / "src"
    assert session_path(wt) == repo.resolve()


def test_session_path_maps_linked_worktree_inside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = repo / ".claude" / "worktrees" / "wt"
    _fake_linked_worktree(repo, wt)
    assert session_path(wt / "src") == repo.resolve() / "src"


def test_session_path_relative_gitdir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wt = repo / ".claude" / "worktrees" / "wt"
    _fake_linked_worktree(repo, wt)
    (wt / ".git").write_text("gitdir: ../../../.git/worktrees/wt\n")
    assert session_path(wt / "src") == repo.resolve() / "src"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_session_path_real_git_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "commit", "--allow-empty", "-m", "init")
    wt = tmp_path / "elsewhere" / "wt"
    _git(repo, "worktree", "add", "-b", "wt", str(wt))
    assert session_path(wt) == repo.resolve()
    assert session_path(repo) == repo.resolve()


def test_session_path_plain_dir_and_missing_dir(tmp_path: Path) -> None:
    assert session_path(tmp_path) == tmp_path.resolve()
    assert session_path(tmp_path / "gone") == (tmp_path / "gone").resolve()  # never raises


def test_session_path_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert session_path("~/proj") == (tmp_path / "proj").resolve()


def test_session_path_broken_git_file_falls_back(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("not a gitdir pointer\n")
    assert session_path(tmp_path) == tmp_path.resolve()
    (tmp_path / ".git").write_text("gitdir: /nonexistent/x\n")
    assert session_path(tmp_path) == tmp_path.resolve()


def test_session_path_submodule_is_not_mapped(tmp_path: Path) -> None:
    sup = tmp_path / "super"
    (sup / ".git" / "modules" / "sub").mkdir(parents=True)
    sub = sup / "sub"
    (sub / "src").mkdir(parents=True)
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
    assert session_path(sub / "src") == (sub / "src").resolve()


def test_member_helpers() -> None:
    groups = {"g": _g("g", ["a", "b"])}
    assert with_member_renamed(groups, "a", "z")["g"].accounts == ("z", "b")
    assert without_member(groups, "a")["g"].accounts == ("b",)
    assert group_of(groups, "b") == "g"
    assert group_of(groups, "q") is None
    assert groups["g"].accounts == ("a", "b")  # inputs are not mutated


def test_group_of_returns_first_sorted_group() -> None:
    groups = {"z": _g("z", ["a"]), "b": _g("b", ["a"])}
    assert group_of(groups, "a") == "b"
