"""`claude-rotate install-sync` — install crontab entries for periodic sync.

Two entries are installed:

- ``*/2 * * * *`` — keeps tokens warm during long idle periods (tokens
  refresh every ~4h, cron notices within 2 min).
- ``@reboot`` (with a short sleep so the network stack is up) — covers
  the case where the user starts ``claude`` seconds after boot, before
  the periodic schedule has had a chance to fire.

Idempotent. Safe to run multiple times. Uses a marker comment
(``# [claude-rotate:sync]``) on every line we own so we can replace or
remove them without touching other user entries.

Usage:
  claude-rotate install-sync             # install / update
  claude-rotate install-sync --uninstall # remove
"""

from __future__ import annotations

import contextlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from claude_rotate.config import Paths

CRON_TAG = "# [claude-rotate:sync]"
CRON_SCHEDULE_PERIODIC = "*/2 * * * *"
CRON_SCHEDULE_REBOOT = "@reboot"
# Delay after boot before the first refresh — lets the network stack
# come up. 30s is comfortable on consumer hardware; CI/container images
# typically have the network ready before cron even starts.
REBOOT_DELAY_SECONDS = 30
HOOK_SHIM_NAME = "claude-rotate-hook"
HOOK_COMMANDS = ("hook session-start", "hook user-prompt-submit", HOOK_SHIM_NAME)


def build_cron_lines(binary: str, state_dir: Path) -> list[str]:
    """Build the crontab lines for the sync jobs, with markers at end."""
    log = state_dir / "sync.log"
    periodic = f"{CRON_SCHEDULE_PERIODIC} {binary} sync-credentials >>{log} 2>&1  {CRON_TAG}"
    reboot = (
        f"{CRON_SCHEDULE_REBOOT} sleep {REBOOT_DELAY_SECONDS} && "
        f"{binary} sync-credentials >>{log} 2>&1  {CRON_TAG}"
    )
    return [periodic, reboot]


def merge_crontab(existing: str, new_lines: list[str], *, remove: bool) -> tuple[str, bool]:
    """Merge new_lines into existing crontab text.

    Returns (merged_text, changed).
    - If remove=True: strip any line carrying CRON_TAG, ignore new_lines.
    - Else: drop every existing CRON_TAG line, then append all new_lines.
      No-op if the tagged lines already match new_lines exactly.
    """
    lines = existing.splitlines()
    kept: list[str] = [line for line in lines if CRON_TAG not in line]
    existing_tagged = [line for line in lines if CRON_TAG in line]

    if remove:
        if not existing_tagged:
            return existing, False
        return "\n".join(kept) + ("\n" if kept else ""), True

    # install / replace
    if existing_tagged == new_lines:
        return existing, False
    kept.extend(new_lines)
    return "\n".join(kept) + "\n", True


def build_hook_settings(hook_shim: str) -> dict[str, list[dict[str, Any]]]:
    """Build Claude Code hook settings for session binding and prompt guard."""
    return {
        "SessionStart": [
            {
                "matcher": "startup|resume|clear|compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{hook_shim} session-start",
                        "timeout": 5,
                    }
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{hook_shim} user-prompt-submit",
                        "timeout": 5,
                        "statusMessage": "claude-rotate checking session account",
                    }
                ],
            }
        ],
    }


def build_hook_shim_script(
    hook_binary: str,
    hook_subcommand: str | None,
    state_dir: Path,
) -> str:
    """Build a tiny POSIX shell shim for Claude Code hooks.

    The hot path is intentionally shell-only:
    - no ``CLAUDE_ROTATE_GUARD``: drain stdin and exit.
    - prompt belongs to the currently active account: exit.

    Python starts only for session binding, unregistered sessions, or account
    mismatches where the expensive-context guard needs full metadata.
    """
    run_hook = (
        f"{shlex.quote(hook_binary)} {shlex.quote(hook_subcommand)} \"$@\""
        if hook_subcommand
        else f"{shlex.quote(hook_binary)} \"$@\""
    )
    run_hook_with_payload = (
        f"{shlex.quote(hook_binary)} {shlex.quote(hook_subcommand)} \"$event\""
        if hook_subcommand
        else f"{shlex.quote(hook_binary)} \"$event\""
    )
    quoted_state = shlex.quote(str(state_dir))
    return f"""#!/bin/sh
case "${{CLAUDE_ROTATE_GUARD:-}}" in
  1|true|TRUE|yes|YES|on|ON) ;;
  *) cat >/dev/null; exit 0 ;;
esac

event="${{1:-}}"
state_dir={quoted_state}

if [ "$event" = "user-prompt-submit" ]; then
  payload=$(cat)
  session_id=$(
    printf '%s' "$payload" |
      sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p'
  )
  case "$session_id" in
    ""|*[!A-Za-z0-9_.-]*) ;;
    *)
      record="$state_dir/sessions/$session_id.json"
      current="$state_dir/current-session.json"
      if [ -r "$record" ] && [ -r "$current" ]; then
        bound=$(
          sed -n 's/.*"account_name"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' \
            "$record" |
            head -n 1
        )
        active=$(
          sed -n 's/.*"account_name"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' \
            "$current" |
            head -n 1
        )
        if [ -n "$bound" ] && [ "$bound" = "$active" ]; then
          exit 0
        fi
      fi
      ;;
  esac
  printf '%s' "$payload" | {run_hook_with_payload}
  exit $?
fi

exec {run_hook}
"""


def write_hook_shim(paths: Paths, hook_binary: str, hook_subcommand: str | None) -> Path:
    """Write the fast hook shim and return its path."""
    shim = paths.state_dir / HOOK_SHIM_NAME
    shim.write_text(build_hook_shim_script(hook_binary, hook_subcommand, paths.state_dir))
    shim.chmod(0o755)
    return shim


def merge_hook_settings(
    existing: dict[str, Any],
    hook_shim: str,
    *,
    remove: bool,
) -> tuple[dict[str, Any], bool]:
    """Merge claude-rotate hooks into Claude Code user settings."""
    before = json.dumps(existing, sort_keys=True)
    merged = json.loads(json.dumps(existing))
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        merged["hooks"] = hooks

    for event in ("SessionStart", "UserPromptSubmit"):
        groups = hooks.get(event)
        if isinstance(groups, list):
            cleaned = [_clean_hook_group(group) for group in groups if isinstance(group, dict)]
            cleaned = [group for group in cleaned if group.get("hooks")]
        else:
            cleaned = []
        if cleaned:
            hooks[event] = cleaned
        else:
            hooks.pop(event, None)

    if not remove:
        ours = build_hook_settings(hook_shim)
        for event, groups in ours.items():
            hooks.setdefault(event, [])
            hooks[event].extend(groups)

    if not hooks:
        merged.pop("hooks", None)

    after = json.dumps(merged, sort_keys=True)
    return merged, before != after


def _clean_hook_group(group: dict[str, Any]) -> dict[str, Any]:
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return group
    cleaned_hooks = []
    for hook in hooks:
        if not isinstance(hook, dict):
            cleaned_hooks.append(hook)
            continue
        command = hook.get("command")
        if isinstance(command, str) and any(marker in command for marker in HOOK_COMMANDS):
            continue
        cleaned_hooks.append(hook)
    out = dict(group)
    out["hooks"] = cleaned_hooks
    return out


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def execute(paths: Paths, *, uninstall: bool) -> int:
    binary = shutil.which("claude-rotate")
    if not binary:
        print("error: claude-rotate is not on PATH", file=sys.stderr)
        return 1
    hook_binary = shutil.which("claude-rotate-hook")
    hook_subcommand = None
    if not hook_binary:
        hook_binary = binary
        hook_subcommand = "hook"

    paths.state_dir.mkdir(parents=True, exist_ok=True)
    hook_shim = paths.state_dir / HOOK_SHIM_NAME
    if uninstall:
        with contextlib.suppress(OSError):
            hook_shim.unlink()
    else:
        hook_shim = write_hook_shim(paths, hook_binary, hook_subcommand)

    new_lines = build_cron_lines(binary, paths.state_dir)

    # Read current crontab — exit 1 when the user has none (no crontab)
    read = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    existing = read.stdout if read.returncode == 0 else ""

    merged, cron_changed = merge_crontab(existing, new_lines, remove=uninstall)
    if cron_changed:
        write = subprocess.run(
            ["crontab", "-"],
            input=merged,
            capture_output=True,
            text=True,
            check=False,
        )
        if write.returncode != 0:
            print(f"error: crontab write failed: {write.stderr.strip()}", file=sys.stderr)
            return 1

    settings_path = _settings_path()
    settings = _load_settings(settings_path)
    merged_settings, settings_changed = merge_hook_settings(
        settings,
        str(hook_shim),
        remove=uninstall,
    )
    if settings_changed:
        _save_settings(settings_path, merged_settings)

    if not cron_changed and not settings_changed:
        print("sync cron entries and session guard hooks already up to date", file=sys.stderr)
        return 0

    action = "removed" if uninstall else "installed"
    print(
        f"sync cron entries {action} ({CRON_SCHEDULE_PERIODIC} + {CRON_SCHEDULE_REBOOT}); "
        f"Claude Code session guard hooks {action}",
        file=sys.stderr,
    )
    return 0
