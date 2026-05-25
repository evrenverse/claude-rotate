from __future__ import annotations

from pathlib import Path

import pytest

from claude_rotate.config import paths
from claude_rotate.settings import RotateConfig, load_config, save_config, set_value


def test_load_config_defaults_when_missing(rotate_dir: Path) -> None:
    cfg = load_config(paths())
    assert cfg == RotateConfig()
    assert cfg.session_isolation is False
    assert cfg.auto_resume_enabled is False
    assert cfg.auto_resume_message == "weiter gehts"


def test_load_config_reads_existing(rotate_dir: Path) -> None:
    p = paths()
    p.config_dir.mkdir(parents=True, exist_ok=True)
    p.config_file.write_text(
        '{"session_isolation": true, '
        '"auto_resume": {"enabled": true, "message": "go on"}}\n'
    )
    cfg = load_config(p)
    assert cfg.session_isolation is True
    assert cfg.auto_resume_enabled is True
    assert cfg.auto_resume_message == "go on"


def test_load_config_partial_uses_defaults(rotate_dir: Path) -> None:
    p = paths()
    p.config_dir.mkdir(parents=True, exist_ok=True)
    p.config_file.write_text('{"session_isolation": true}\n')
    cfg = load_config(p)
    assert cfg.session_isolation is True
    assert cfg.auto_resume_enabled is False
    assert cfg.auto_resume_message == "weiter gehts"


def test_load_config_corrupt_returns_defaults(rotate_dir: Path) -> None:
    p = paths()
    p.config_dir.mkdir(parents=True, exist_ok=True)
    p.config_file.write_text("{not json")
    assert load_config(p) == RotateConfig()


def test_save_round_trips(rotate_dir: Path) -> None:
    p = paths()
    cfg = RotateConfig(session_isolation=True, auto_resume_enabled=True, auto_resume_message="x")
    save_config(p, cfg)
    assert load_config(p) == cfg
    assert (p.config_file.stat().st_mode & 0o777) == 0o600


def test_set_value_booleans(rotate_dir: Path) -> None:
    p = paths()
    set_value(p, "session_isolation", "true")
    assert load_config(p).session_isolation is True
    set_value(p, "session_isolation", "false")
    assert load_config(p).session_isolation is False
    set_value(p, "auto_resume.enabled", "1")
    assert load_config(p).auto_resume_enabled is True


def test_set_value_message(rotate_dir: Path) -> None:
    p = paths()
    set_value(p, "auto_resume.message", "los gehts")
    assert load_config(p).auto_resume_message == "los gehts"


def test_set_value_unknown_key_raises(rotate_dir: Path) -> None:
    from claude_rotate.errors import ConfigError

    with pytest.raises(ConfigError, match="unknown config key"):
        set_value(paths(), "nope", "true")
