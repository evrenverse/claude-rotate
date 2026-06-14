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
        '{"session_isolation": true, "auto_resume": {"enabled": true, "message": "go on"}}\n'
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


def test_env_overrides_session_isolation(rotate_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = paths()
    save_config(p, RotateConfig(session_isolation=False))
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION_ISOLATION", "1")
    assert load_config(p).session_isolation is True
    monkeypatch.setenv("CLAUDE_ROTATE_SESSION_ISOLATION", "0")
    assert load_config(p).session_isolation is False
    monkeypatch.delenv("CLAUDE_ROTATE_SESSION_ISOLATION", raising=False)
    assert load_config(p).session_isolation is False


def test_session_tracking_defaults_on_and_roundtrips(tmp_path: Path) -> None:
    from claude_rotate.config import Paths
    from claude_rotate.settings import load_config, set_value

    p = Paths(config_dir=tmp_path / "c", cache_dir=tmp_path / "ca", state_dir=tmp_path / "s")
    # Default: ON even with no config.json.
    assert load_config(p).session_tracking is True
    set_value(p, "session_tracking", "false")
    assert load_config(p).session_tracking is False


def test_setting_other_key_preserves_session_tracking(tmp_path: Path) -> None:
    from claude_rotate.config import Paths
    from claude_rotate.settings import load_config, set_value

    p = Paths(config_dir=tmp_path / "c", cache_dir=tmp_path / "ca", state_dir=tmp_path / "s")
    set_value(p, "session_tracking", "false")
    # Changing an unrelated key must NOT reset session_tracking back to its default.
    set_value(p, "session_isolation", "true")
    cfg = load_config(p)
    assert cfg.session_tracking is False
    assert cfg.session_isolation is True
