from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from claude_rotate.commands.install_sync import (
    CRON_TAG,
    build_cron_line,
    execute,
    merge_crontab,
)
from claude_rotate.config import Paths


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def test_build_cron_line_contains_marker_and_command() -> None:
    line = build_cron_line("/usr/local/bin/claude-rotate", Path("/home/u/.local/state/cr"))
    assert CRON_TAG in line
    assert "*/2 * * * *" in line
    assert "claude-rotate sync-credentials" in line
    assert "/home/u/.local/state/cr/sync.log" in line


def test_merge_crontab_adds_new_entry() -> None:
    existing = "# my other job\n0 * * * * /usr/bin/foo\n"
    new_line = "*/2 * * * * /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG
    merged, changed = merge_crontab(existing, new_line, remove=False)
    assert changed is True
    assert "foo" in merged
    assert new_line in merged


def test_merge_crontab_replaces_existing_entry() -> None:
    old_line = "*/5 * * * * /old/path/claude-rotate sync-credentials  " + CRON_TAG
    existing = f"# my other job\n0 * * * * /usr/bin/foo\n{old_line}\n"
    new_line = "*/2 * * * * /new/path/claude-rotate sync-credentials  " + CRON_TAG
    merged, changed = merge_crontab(existing, new_line, remove=False)
    assert changed is True
    assert old_line not in merged
    assert new_line in merged
    assert merged.count(CRON_TAG) == 1  # only one tagged line


def test_merge_crontab_idempotent_when_identical() -> None:
    line = "*/2 * * * * /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG
    existing = f"# comment\n{line}\n"
    _merged, changed = merge_crontab(existing, line, remove=False)
    assert changed is False


def test_merge_crontab_removes_entry_when_remove_true() -> None:
    line = "*/2 * * * * /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG
    existing = f"# comment\n{line}\n0 * * * * /usr/bin/foo\n"
    merged, _changed = merge_crontab(existing, line, remove=True)
    assert line not in merged
    assert "foo" in merged


def test_execute_installs_when_crontab_writable(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)

    monkeypatch.setattr(
        "claude_rotate.commands.install_sync.shutil.which",
        lambda name: "/usr/local/bin/claude-rotate" if name == "claude-rotate" else None,
    )

    calls: dict[str, str] = {}

    def fake_run(args, **kw):
        if args[:2] == ["crontab", "-l"]:
            return MagicMock(returncode=0, stdout="# existing\n", stderr="")
        if args == ["crontab", "-"]:
            calls["install_input"] = kw.get("input", "")
            return MagicMock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected call: {args}")

    monkeypatch.setattr("claude_rotate.commands.install_sync.subprocess.run", fake_run)

    rc = execute(p, uninstall=False)
    assert rc == 0
    assert CRON_TAG in calls["install_input"]
    assert "claude-rotate sync-credentials" in calls["install_input"]
