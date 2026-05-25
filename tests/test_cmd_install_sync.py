from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_rotate.commands.install_sync import (
    CRON_TAG,
    build_cron_lines,
    build_hook_settings,
    build_hook_shim_script,
    execute,
    merge_crontab,
    merge_hook_settings,
)
from claude_rotate.config import Paths


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


@pytest.fixture(autouse=True)
def _isolate_user_claude_settings(tmp_path, monkeypatch) -> None:
    settings_path = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(
        "claude_rotate.commands.install_sync._settings_path",
        lambda: settings_path,
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


def test_build_hook_settings_installs_session_guard_commands() -> None:
    hook_shim = "/home/u/.local/state/claude-rotate/claude-rotate-hook"
    hooks = build_hook_settings(hook_shim)
    session = hooks["SessionStart"][0]["hooks"][0]
    prompt = hooks["UserPromptSubmit"][0]["hooks"][0]
    assert session["command"] == f"{hook_shim} session-start"
    assert prompt["command"] == f"{hook_shim} user-prompt-submit"
    assert prompt["type"] == "command"
    assert prompt["timeout"] == 5


def test_hook_shim_exits_without_python_when_guard_disabled(tmp_path) -> None:
    log = tmp_path / "hook-called"
    hook_bin = tmp_path / "claude-rotate-hook-bin"
    hook_bin.write_text(f"#!/bin/sh\necho called >> {log}\n")
    hook_bin.chmod(0o755)
    shim = tmp_path / "claude-rotate-hook"
    shim.write_text(build_hook_shim_script(str(hook_bin), None, tmp_path / "state"))
    shim.chmod(0o755)

    result = subprocess.run(
        [str(shim), "user-prompt-submit"],
        input='{"session_id":"sid"}',
        text=True,
        capture_output=True,
        check=False,
        env={},
    )

    assert result.returncode == 0
    assert not log.exists()


def test_hook_shim_fast_allows_same_account_prompt(tmp_path) -> None:
    log = tmp_path / "hook-called"
    state = tmp_path / "state"
    sessions = state / "sessions"
    sessions.mkdir(parents=True)
    (state / "current-session.json").write_text('{"account_name": "matri"}\n')
    (sessions / "sid.json").write_text('{"account_name": "matri"}\n')
    hook_bin = tmp_path / "claude-rotate-hook-bin"
    hook_bin.write_text(f"#!/bin/sh\necho called >> {log}\n")
    hook_bin.chmod(0o755)
    shim = tmp_path / "claude-rotate-hook"
    shim.write_text(build_hook_shim_script(str(hook_bin), None, state))
    shim.chmod(0o755)

    result = subprocess.run(
        [str(shim), "user-prompt-submit"],
        input='{"session_id":"sid"}',
        text=True,
        capture_output=True,
        check=False,
        env={"CLAUDE_ROTATE_GUARD": "1"},
    )

    assert result.returncode == 0
    assert not log.exists()


def test_hook_shim_forwards_mismatch_to_python_hook(tmp_path) -> None:
    log = tmp_path / "hook-called"
    forwarded = tmp_path / "payload.json"
    state = tmp_path / "state"
    sessions = state / "sessions"
    sessions.mkdir(parents=True)
    (state / "current-session.json").write_text('{"account_name": "flavius"}\n')
    (sessions / "sid.json").write_text('{"account_name": "matri"}\n')
    hook_bin = tmp_path / "claude-rotate-hook-bin"
    hook_bin.write_text(f"#!/bin/sh\necho \"$@\" > {log}\ncat > {forwarded}\n")
    hook_bin.chmod(0o755)
    shim = tmp_path / "claude-rotate-hook"
    shim.write_text(build_hook_shim_script(str(hook_bin), None, state))
    shim.chmod(0o755)

    result = subprocess.run(
        [str(shim), "user-prompt-submit"],
        input='{"session_id":"sid"}',
        text=True,
        capture_output=True,
        check=False,
        env={"CLAUDE_ROTATE_GUARD": "1"},
    )

    assert result.returncode == 0
    assert log.read_text().strip() == "user-prompt-submit"
    assert forwarded.read_text() == '{"session_id":"sid"}'


def test_merge_hook_settings_preserves_other_hooks_and_adds_ours() -> None:
    existing = {
        "model": "opus",
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": "/usr/bin/other-hook"},
                    ]
                }
            ]
        },
    }
    merged, changed = merge_hook_settings(existing, "/state/claude-rotate-hook", remove=False)
    assert changed is True
    prompt_handlers = merged["hooks"]["UserPromptSubmit"]
    commands = [
        hook["command"]
        for group in prompt_handlers
        for hook in group["hooks"]
        if hook.get("type") == "command"
    ]
    assert "/usr/bin/other-hook" in commands
    assert "/state/claude-rotate-hook user-prompt-submit" in commands


def test_merge_hook_settings_replaces_existing_claude_rotate_hooks() -> None:
    existing = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": "/old/claude-rotate hook session-start"},
                    ]
                }
            ],
        }
    }
    merged, changed = merge_hook_settings(existing, "/state/claude-rotate-hook", remove=False)
    assert changed is True
    commands = [
        hook["command"]
        for group in merged["hooks"]["SessionStart"]
        for hook in group["hooks"]
        if hook.get("type") == "command"
    ]
    assert "/old/claude-rotate hook session-start" not in commands
    assert "/state/claude-rotate-hook session-start" in commands


def test_merge_hook_settings_removes_claude_rotate_hooks_only() -> None:
    existing = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {"type": "command", "command": "/old/claude-rotate hook session-start"},
                        {"type": "command", "command": "/usr/bin/other-hook"},
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/old/claude-rotate hook user-prompt-submit",
                        },
                    ]
                }
            ],
        }
    }
    merged, changed = merge_hook_settings(existing, "/state/claude-rotate-hook", remove=True)
    assert changed is True
    assert "UserPromptSubmit" not in merged["hooks"]
    commands = [
        hook["command"]
        for group in merged["hooks"]["SessionStart"]
        for hook in group["hooks"]
        if hook.get("type") == "command"
    ]
    assert commands == ["/usr/bin/other-hook"]


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


def test_execute_installs_hooks_even_when_cron_is_current(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    cron_lines = build_cron_lines("/usr/local/bin/claude-rotate", p.state_dir)

    monkeypatch.setattr(
        "claude_rotate.commands.install_sync.shutil.which",
        lambda name: "/usr/local/bin/claude-rotate" if name == "claude-rotate" else None,
    )
    monkeypatch.setattr(
        "claude_rotate.commands.install_sync._settings_path",
        lambda: settings_path,
    )

    def fake_run(args, **_kw):
        if args[:2] == ["crontab", "-l"]:
            return MagicMock(returncode=0, stdout="\n".join(cron_lines) + "\n", stderr="")
        if args == ["crontab", "-"]:
            raise AssertionError("crontab should not be rewritten")
        raise AssertionError(f"unexpected call: {args}")

    monkeypatch.setattr("claude_rotate.commands.install_sync.subprocess.run", fake_run)

    rc = execute(p, uninstall=False)

    assert rc == 0
    settings = json.loads(settings_path.read_text())
    commands = [
        hook["command"]
        for groups in settings["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert str(p.state_dir / "claude-rotate-hook") + " session-start" in commands
    assert str(p.state_dir / "claude-rotate-hook") + " user-prompt-submit" in commands
