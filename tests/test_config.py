from __future__ import annotations

from pathlib import Path

import pytest

from claude_rotate import config


def test_monolithic_override_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    p = config.paths()
    assert p.config_dir == tmp_path / "config"
    assert p.cache_dir == tmp_path / "cache"
    assert p.state_dir == tmp_path / "state"


def test_paths_are_platformdirs_when_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_ROTATE_DIR", raising=False)
    p = config.paths()
    assert p.config_dir.name == "claude-rotate"
    assert p.cache_dir.name == "claude-rotate"


def test_ensure_dirs_creates_and_chmods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    p = config.paths()
    config.ensure_dirs(p)
    assert p.config_dir.is_dir()
    assert p.cache_dir.is_dir()
    assert (p.config_dir.stat().st_mode & 0o777) == 0o700


def test_accounts_file_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    p = config.paths()
    assert p.accounts_file == tmp_path / "config" / "accounts.json"
    assert p.lock_file == tmp_path / "config" / "accounts.json.lock"
    assert p.usage_dir == tmp_path / "cache" / "usage"
    assert p.log_file == tmp_path / "state" / "log.jsonl"
