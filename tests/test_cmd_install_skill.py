"""Tests for `claude-rotate install-skill` (cross-agent symlink install)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from claude_rotate.commands.install_skill import (
    SKILL_NAME,
    bundled_file,
    canonical_dir,
    execute,
    install,
    remove,
)
from claude_rotate.config import Paths


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def _fake_agents(home: Path, *names: str) -> dict[str, Path]:
    """Create agent home dirs under a fake HOME; return their skills dirs."""
    layout = {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "gemini": home / ".gemini",
        "opencode": home / ".config" / "opencode",
    }
    skills: dict[str, Path] = {}
    for name in names:
        layout[name].mkdir(parents=True, exist_ok=True)
        skills[name] = layout[name] / "skills"
    return skills


def test_bundled_skill_is_packaged_and_well_formed() -> None:
    text = bundled_file("SKILL.md")
    assert text.startswith("---")
    assert f"name: {SKILL_NAME}" in text
    # the skill must drive the CLI report, not a loose script
    assert "claude-rotate status --report" in text


def test_install_writes_canonical_and_symlinks_present_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skills = _fake_agents(tmp_path, "claude", "codex")

    canonical, linked = install()

    # canonical copy holds the real file
    assert canonical == tmp_path / ".agents" / "skills" / SKILL_NAME
    assert (canonical / "SKILL.md").read_text(encoding="utf-8") == bundled_file("SKILL.md")

    labels = {label for label, _ in linked}
    assert labels == {"Claude Code", "Codex"}

    for agent in ("claude", "codex"):
        link = skills[agent] / SKILL_NAME
        assert link.is_symlink()
        # relative target, resolving to the canonical dir
        assert link.resolve() == canonical.resolve()
        assert (link / "SKILL.md").exists()


def test_install_skips_absent_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _fake_agents(tmp_path, "claude")  # only Claude present

    _canonical, linked = install()

    assert [label for label, _ in linked] == ["Claude Code"]
    assert not (tmp_path / ".codex" / "skills" / SKILL_NAME).exists()
    assert not (tmp_path / ".gemini" / "skills" / SKILL_NAME).exists()


def test_install_replaces_existing_real_dir_with_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skills = _fake_agents(tmp_path, "claude")
    stale = skills["claude"] / SKILL_NAME
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("OLD", encoding="utf-8")

    install()

    link = skills["claude"] / SKILL_NAME
    assert link.is_symlink()
    assert (link / "SKILL.md").read_text(encoding="utf-8") == bundled_file("SKILL.md")


def test_symlink_target_is_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skills = _fake_agents(tmp_path, "codex")
    install()
    target = os.readlink(skills["codex"] / SKILL_NAME)
    assert not os.path.isabs(target)
    assert target == "../../.agents/skills/account"


def test_execute_then_uninstall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skills = _fake_agents(tmp_path, "claude", "codex")

    assert execute(_paths(tmp_path), uninstall=False) == 0
    assert (skills["claude"] / SKILL_NAME).is_symlink()
    assert canonical_dir().exists()

    assert execute(_paths(tmp_path), uninstall=True) == 0
    assert not (skills["claude"] / SKILL_NAME).exists()
    assert not (skills["codex"] / SKILL_NAME).exists()
    assert not canonical_dir().exists()


def test_install_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skills = _fake_agents(tmp_path, "claude")
    assert execute(_paths(tmp_path), uninstall=False) == 0
    assert execute(_paths(tmp_path), uninstall=False) == 0
    assert (skills["claude"] / SKILL_NAME).is_symlink()


def test_remove_when_absent_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _fake_agents(tmp_path, "claude")
    assert remove() == []
