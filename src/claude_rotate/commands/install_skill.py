"""`claude-rotate install-skill` — deploy the bundled agent skill cross-agent.

Skills follow the shared convention used on multi-agent setups: the skill lives
once in a canonical store (``~/.agents/skills/<name>``) and each installed agent
gets a *symlink* into it from its own skills directory. That way Claude Code,
Codex, Gemini and opencode all see the same skill, and a single edit updates
them all.

The skill ships as package data (``claude_rotate/skill_assets``) and is read via
``importlib.resources``, so it works the same whether claude-rotate was
installed with uv, pipx, or from a checkout.

Idempotent. Safe to re-run to update.

Usage:
  claude-rotate install-skill              # install / update (all detected agents)
  claude-rotate install-skill --uninstall  # remove
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path

from claude_rotate.config import Paths

SKILL_NAME = "account"
_SKILL_FILES = ("SKILL.md",)


def canonical_dir() -> Path:
    """The single source-of-truth location for the skill, shared across agents."""
    return Path.home() / ".agents" / "skills" / SKILL_NAME


def agent_targets() -> list[tuple[str, Path]]:
    """(label, skills-dir) for every agent whose home is present on this machine."""
    home = Path.home()
    candidates: list[tuple[str, Path, Path]] = [
        ("Claude Code", home / ".claude", home / ".claude" / "skills"),
        ("Codex", home / ".codex", home / ".codex" / "skills"),
        ("Gemini", home / ".gemini", home / ".gemini" / "skills"),
        ("opencode", home / ".config" / "opencode", home / ".config" / "opencode" / "skills"),
    ]
    return [(label, skills) for label, agent_home, skills in candidates if agent_home.exists()]


def bundled_file(name: str) -> str:
    """Read a packaged skill asset by file name."""
    resource = files("claude_rotate").joinpath("skill_assets", SKILL_NAME, name)
    return resource.read_text(encoding="utf-8")


def _link_into(skills_dir: Path, canonical: Path) -> Path:
    """Create (or refresh) a relative symlink ``skills_dir/<name>`` → canonical."""
    skills_dir.mkdir(parents=True, exist_ok=True)
    link = skills_dir / SKILL_NAME
    if link.is_symlink() or link.exists():
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
        else:
            link.unlink()
    link.symlink_to(os.path.relpath(canonical, skills_dir), target_is_directory=True)
    return link


def install() -> tuple[Path, list[tuple[str, Path]]]:
    """Write the canonical skill and symlink it into every detected agent."""
    canonical = canonical_dir()
    canonical.mkdir(parents=True, exist_ok=True)
    for name in _SKILL_FILES:
        (canonical / name).write_text(bundled_file(name), encoding="utf-8")
    linked = [(label, _link_into(skills, canonical)) for label, skills in agent_targets()]
    return canonical, linked


def remove() -> list[Path]:
    """Remove the per-agent symlinks and the canonical skill directory."""
    removed: list[Path] = []
    for _label, skills in agent_targets():
        link = skills / SKILL_NAME
        if link.is_symlink():
            link.unlink()
            removed.append(link)
        elif link.is_dir():
            shutil.rmtree(link)
            removed.append(link)
    canonical = canonical_dir()
    if canonical.exists():
        shutil.rmtree(canonical)
        removed.append(canonical)
    return removed


def execute(paths: Paths, *, uninstall: bool) -> int:
    if uninstall:
        removed = remove()
        if removed:
            print("  Removed 'account' skill:", file=sys.stderr)
            for path in removed:
                print(f"    {path}", file=sys.stderr)
        else:
            print("  Nothing to remove.", file=sys.stderr)
        return 0

    canonical, linked = install()
    print(f"  Installed 'account' skill → {canonical}", file=sys.stderr)
    if linked:
        for label, link in linked:
            print(f"    linked into {label}: {link}", file=sys.stderr)
        print("  Invoke it as /account (or ask which account is active).", file=sys.stderr)
    else:
        print(
            "  No agents detected (~/.claude, ~/.codex, ~/.gemini, ~/.config/opencode).",
            file=sys.stderr,
        )
    return 0
