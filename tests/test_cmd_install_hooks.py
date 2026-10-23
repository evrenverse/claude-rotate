from __future__ import annotations

import json
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_rotate.commands.install_hooks import HOOK_SPEC, install, remove
from claude_rotate.errors import ConfigError


def test_install_adds_all_hooks_idempotently(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "existing"}]}]
                }
            }
        )
    )

    install(settings)
    install(settings)  # second run must not duplicate

    data = json.loads(settings.read_text())
    cmds = [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
    # our hook commands present exactly once each, plus the pre-existing one kept
    for _event, command in HOOK_SPEC:
        assert cmds.count(command) == 1
    assert "existing" in cmds


def test_remove_strips_only_our_hooks(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "existing"}]}]
                }
            }
        )
    )
    install(settings)
    remove(settings)

    data = json.loads(settings.read_text())
    cmds = [
        h["command"]
        for groups in data.get("hooks", {}).values()
        for g in groups
        for h in g["hooks"]
    ]
    assert "existing" in cmds
    assert not any(c.startswith("claude-rotate __heartbeat") for c in cmds)


def test_install_into_fresh_file_adds_both_hooks(tmp_path: Path) -> None:
    # The common first-install path: no settings.json yet.
    settings = tmp_path / "settings.json"
    install(settings)
    data = json.loads(settings.read_text())
    cmds = [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
    assert "claude-rotate __heartbeat active" in cmds
    assert "claude-rotate __heartbeat end" in cmds


def test_install_refuses_corrupt_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    with pytest.raises(ConfigError):
        install(settings)


_WRITERS = [pytest.param(install, id="install"), pytest.param(remove, id="remove")]

# Deliberately unsorted, with non-ASCII and a secret-bearing env.
_USER_SETTINGS = {
    "permissions": {"deny": ["Bash(rm:*)"], "allow": ["Read", "Bash(ls:*)"]},
    "env": {"ZED": "1", "ALPHA": "2"},
    "statusLine": {"type": "command", "command": "echo — ü"},
    "hooks": {
        "SessionEnd": [{"hooks": [{"type": "command", "command": "claude-rotate __heartbeat end"}]}]
    },
    "alwaysThinkingEnabled": True,
}


def _seed(path: Path, mode: int | None = None) -> None:
    path.write_text(
        json.dumps(_USER_SETTINGS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if mode is not None:
        path.chmod(mode)


@pytest.mark.parametrize("write", _WRITERS)
def test_write_keeps_key_order(tmp_path: Path, write: Callable[[Path], None]) -> None:
    settings = tmp_path / "settings.json"
    _seed(settings)
    write(settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert list(data) == list(_USER_SETTINGS)
    assert list(data["permissions"]) == ["deny", "allow"]
    assert list(data["env"]) == ["ZED", "ALPHA"]


@pytest.mark.parametrize("write", _WRITERS)
def test_write_keeps_non_ascii_literal(tmp_path: Path, write: Callable[[Path], None]) -> None:
    settings = tmp_path / "settings.json"
    _seed(settings)
    write(settings)
    raw = settings.read_text(encoding="utf-8")
    assert "echo — ü" in raw
    assert "\\u" not in raw
    assert json.loads(raw)["statusLine"] == _USER_SETTINGS["statusLine"]


@pytest.mark.parametrize("write", _WRITERS)
@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_write_keeps_file_mode(tmp_path: Path, write: Callable[[Path], None], mode: int) -> None:
    settings = tmp_path / "settings.json"
    _seed(settings, mode)
    write(settings)
    assert stat.S_IMODE(settings.stat().st_mode) == mode


def test_install_creates_fresh_file_private(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    install(settings)
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600


@pytest.mark.parametrize("write", _WRITERS)
def test_write_through_symlink_keeps_link(tmp_path: Path, write: Callable[[Path], None]) -> None:
    real = tmp_path / "real" / "settings.json"
    real.parent.mkdir()
    _seed(real)
    link = tmp_path / "settings.json"
    link.symlink_to(real)

    write(link)

    assert link.is_symlink()
    assert link.readlink() == real
    cmds = [
        h["command"]
        for groups in json.loads(real.read_text(encoding="utf-8"))["hooks"].values()
        for g in groups
        for h in g["hooks"]
    ]
    assert ("claude-rotate __heartbeat active" in cmds) is (write is install)
    assert ("claude-rotate __heartbeat end" in cmds) is (write is install)
