from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from claude_rotate.commands.install_sync import (
    CRON_TAG,
    build_cron_lines,
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


def test_build_cron_lines_covers_periodic_and_reboot() -> None:
    lines = build_cron_lines("/usr/local/bin/claude-rotate", Path("/home/u/.local/state/cr"))
    assert len(lines) == 2
    periodic, reboot = lines
    assert CRON_TAG in periodic
    assert CRON_TAG in reboot
    assert "*/2 * * * *" in periodic
    assert periodic.count("claude-rotate sync-credentials") == 1
    assert reboot.startswith("@reboot")
    assert "sleep 30" in reboot  # WLAN grace
    assert reboot.count("claude-rotate sync-credentials") == 1
    assert "/home/u/.local/state/cr/sync.log" in periodic
    assert "/home/u/.local/state/cr/sync.log" in reboot


def test_merge_crontab_adds_both_entries() -> None:
    existing = "# my other job\n0 * * * * /usr/bin/foo\n"
    new_lines = [
        "*/2 * * * * /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG,
        "@reboot sleep 30 && /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG,
    ]
    merged, changed = merge_crontab(existing, new_lines, remove=False)
    assert changed is True
    assert "foo" in merged
    for line in new_lines:
        assert line in merged
    assert merged.count(CRON_TAG) == 2


def test_merge_crontab_replaces_existing_entries() -> None:
    old_lines = [
        "*/5 * * * * /old/claude-rotate sync-credentials  " + CRON_TAG,
        "@reboot /old/claude-rotate sync-credentials  " + CRON_TAG,
    ]
    existing = "# my other job\n0 * * * * /usr/bin/foo\n" + "\n".join(old_lines) + "\n"
    new_lines = [
        "*/2 * * * * /new/claude-rotate sync-credentials  " + CRON_TAG,
        "@reboot sleep 30 && /new/claude-rotate sync-credentials  " + CRON_TAG,
    ]
    merged, changed = merge_crontab(existing, new_lines, remove=False)
    assert changed is True
    for old in old_lines:
        assert old not in merged
    for new in new_lines:
        assert new in merged
    assert merged.count(CRON_TAG) == 2


def test_merge_crontab_idempotent_when_identical() -> None:
    new_lines = [
        "*/2 * * * * /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG,
        "@reboot sleep 30 && /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG,
    ]
    existing = "# comment\n" + "\n".join(new_lines) + "\n"
    _merged, changed = merge_crontab(existing, new_lines, remove=False)
    assert changed is False


def test_merge_crontab_removes_all_tagged_entries() -> None:
    tagged_a = "*/2 * * * * /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG
    tagged_b = "@reboot sleep 30 && /usr/local/bin/claude-rotate sync-credentials  " + CRON_TAG
    existing = f"# comment\n{tagged_a}\n{tagged_b}\n0 * * * * /usr/bin/foo\n"
    merged, changed = merge_crontab(existing, [], remove=True)
    assert changed is True
    assert tagged_a not in merged
    assert tagged_b not in merged
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
    payload = calls["install_input"]
    assert payload.count(CRON_TAG) == 2
    assert "*/2 * * * *" in payload
    assert "@reboot" in payload
    assert "claude-rotate sync-credentials" in payload
