from __future__ import annotations

from pathlib import Path

from claude_rotate.config import paths
from claude_rotate.settings import RotateConfig, load_config


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
