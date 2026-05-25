from __future__ import annotations

from pathlib import Path

from claude_rotate.cli import main
from claude_rotate.config import paths
from claude_rotate.settings import load_config


def test_config_set_and_get(rotate_dir: Path, capsys) -> None:
    assert main(["config", "set", "session_isolation", "true"]) == 0
    assert load_config(paths()).session_isolation is True

    capsys.readouterr()
    assert main(["config", "get", "session_isolation"]) == 0
    assert "true" in capsys.readouterr().out.lower()


def test_config_get_all(rotate_dir: Path, capsys) -> None:
    assert main(["config", "get"]) == 0
    out = capsys.readouterr().out
    assert "session_isolation" in out
    assert "auto_resume.enabled" in out


def test_config_set_unknown_key_errors(rotate_dir: Path, capsys) -> None:
    assert main(["config", "set", "bogus", "true"]) == 1
    assert "unknown config key" in capsys.readouterr().err
