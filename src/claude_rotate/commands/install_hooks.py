"""`claude-rotate install-hooks` — register the heartbeat hook in settings.json.

Merges command hooks into ~/.claude/settings.json idempotently, preserving any
existing hooks. UserPromptSubmit marks the session active; SessionEnd removes its
record. Reverse with --uninstall. (No PreToolUse hook on purpose — it would spawn
a process per tool call and block tool execution.)

Note: in session_isolation mode every per-account config dir symlinks
settings.json back to ~/.claude/settings.json, so a single install covers all
accounts. The hook learns its account/session from the injected env vars.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_rotate.config import Paths
from claude_rotate.errors import ConfigError

# (claude-code hook event, command). These events take no tool matcher.
HOOK_SPEC: list[tuple[str, str]] = [
    ("UserPromptSubmit", "claude-rotate __heartbeat active"),
    ("SessionEnd", "claude-rotate __heartbeat end"),
]

_OUR_PREFIX = "claude-rotate __heartbeat"


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _load(settings_path: Path) -> dict[str, Any]:
    if not settings_path.exists():
        return {}
    try:
        data = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"{settings_path} is unreadable/corrupt; refusing to overwrite: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{settings_path} is not a JSON object; refusing to overwrite.")
    return data


def _save(settings_path: Path, data: dict[str, Any]) -> None:
    # Atomic write: serialise to a temp file in the same dir, then os.replace
    # onto the target so a crash/disk-full mid-write never truncates the user's
    # real settings.json. (Not secret, so no chmod 0600 — unlike credentials.)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(dir=str(settings_path.parent), prefix=".settings.json.tmp-")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        tmp.replace(settings_path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _event_has_command(groups: list[Any], command: str) -> bool:
    return any(
        isinstance(h, dict) and h.get("command") == command
        for g in groups
        if isinstance(g, dict)
        for h in (g.get("hooks", []) if isinstance(g.get("hooks"), list) else [])
    )


def _require_hooks(data: dict[str, Any], settings_path: Path) -> dict[str, list[Any]]:
    """Return the (created-if-absent) hooks mapping, refusing a malformed shape.

    Mirrors ``_load``'s "refusing to overwrite" contract: a structurally-odd but
    valid-JSON settings file yields a clean ConfigError, never an AttributeError.
    """
    raw = data.get("hooks")
    if raw is None:
        hooks: dict[str, list[Any]] = {}
        data["hooks"] = hooks
        return hooks
    if not isinstance(raw, dict):
        raise ConfigError(f"{settings_path} has a non-object 'hooks' section; refusing to modify.")
    return raw


def install(settings_path: Path) -> None:
    data = _load(settings_path)
    hooks = _require_hooks(data, settings_path)
    for event, _command in HOOK_SPEC:
        existing = hooks.get(event)
        if existing is not None and not isinstance(existing, list):
            raise ConfigError(
                f"{settings_path} has a non-list '{event}' hooks entry; refusing to modify."
            )
    for event, command in HOOK_SPEC:
        groups: list[Any] = hooks.setdefault(event, [])
        if not _event_has_command(groups, command):
            groups.append({"hooks": [{"type": "command", "command": command}]})
    _save(settings_path, data)


def remove(settings_path: Path) -> None:
    data = _load(settings_path)
    if not isinstance(data.get("hooks"), dict):
        _save(settings_path, data)
        return
    hooks: dict[str, list[Any]] = data["hooks"]
    for event in list(hooks.keys()):
        groups: list[dict[str, Any]] = []
        for g in hooks[event]:
            if not isinstance(g, dict):
                continue
            raw_inner = g.get("hooks", [])
            kept: list[Any] = [
                h
                for h in (raw_inner if isinstance(raw_inner, list) else [])
                if not isinstance(h, dict) or not str(h.get("command", "")).startswith(_OUR_PREFIX)
            ]
            if kept:
                groups.append({**g, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    _save(settings_path, data)


def execute(paths: Paths, *, uninstall: bool) -> int:
    # paths is unused (CLI symmetry with other commands); the hook path is fixed.
    settings_path = default_settings_path()
    if uninstall:
        remove(settings_path)
        print(f"  Removed heartbeat hooks from {settings_path}", file=sys.stderr)
        return 0
    install(settings_path)
    print(f"  Installed heartbeat hooks → {settings_path}", file=sys.stderr)
    print("  Active/idle session tracking is now precise.", file=sys.stderr)
    return 0
