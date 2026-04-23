"""Tests for `claude-rotate cleanup`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from claude_rotate.accounts import Account, Store
from claude_rotate.commands import cleanup
from claude_rotate.config import Paths


def _paths(root: Path) -> Paths:
    return Paths(
        config_dir=root / "config",
        cache_dir=root / "cache",
        state_dir=root / "state",
    )


def _seed(paths: Paths) -> None:
    """Create all three dirs with realistic content."""
    paths.config_dir.mkdir(parents=True)
    paths.cache_dir.mkdir(parents=True)
    paths.usage_dir.mkdir(parents=True)
    paths.state_dir.mkdir(parents=True)
    Store(paths).save(
        {
            "main": Account(
                name="main",
                runtime_token="sk-ant-oat01-" + "a" * 96,
                label="Max-20 main",
                created_at=datetime(2026, 4, 1, tzinfo=UTC),
                plan="max_20x",
            )
        }
    )
    (paths.usage_dir / "main.json").write_text('{"h5_pct": 33}')
    paths.log_file.write_text('{"event": "exec"}\n')


def test_cleanup_removes_all_three_dirs(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    assert paths.config_dir.exists()
    assert paths.cache_dir.exists()
    assert paths.state_dir.exists()

    rc = cleanup.execute(paths, assume_yes=True)

    assert rc == 0
    assert not paths.config_dir.exists()
    assert not paths.cache_dir.exists()
    assert not paths.state_dir.exists()


def test_cleanup_noop_when_nothing_present(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    # no seeding — nothing exists

    rc = cleanup.execute(paths, assume_yes=True)

    assert rc == 0
    assert "Nothing to clean" in capsys.readouterr().err


def test_cleanup_aborts_on_n(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    _seed(paths)

    with patch("claude_rotate.commands.cleanup.input", return_value="n"):
        rc = cleanup.execute(paths, assume_yes=False)

    assert rc == 1
    assert paths.config_dir.exists()  # nothing deleted
    assert "Aborted" in capsys.readouterr().err


def test_cleanup_proceeds_on_y(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)

    with patch("claude_rotate.commands.cleanup.input", return_value="y"):
        rc = cleanup.execute(paths, assume_yes=False)

    assert rc == 0
    assert not paths.config_dir.exists()


def test_cleanup_aborts_on_ctrl_c(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)

    with patch("claude_rotate.commands.cleanup.input", side_effect=KeyboardInterrupt):
        rc = cleanup.execute(paths, assume_yes=False)

    assert rc == 1
    assert paths.config_dir.exists()


def test_cleanup_refuses_to_follow_symlinks(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    """Defensive: a symlinked target must not be followed by rmtree."""
    paths = _paths(tmp_path)
    # Something the symlink could point at that must survive the test.
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "dont_delete.txt").write_text("precious")
    # Make config_dir a symlink to the protected directory.
    paths.config_dir.symlink_to(protected, target_is_directory=True)
    paths.cache_dir.mkdir(parents=True)
    paths.state_dir.mkdir(parents=True)

    rc = cleanup.execute(paths, assume_yes=True)

    assert rc == 1
    assert "symlink" in capsys.readouterr().err.lower()
    # Both the symlink and its target survive.
    assert paths.config_dir.is_symlink()
    assert (protected / "dont_delete.txt").read_text() == "precious"


def test_cleanup_does_not_touch_home_claude(tmp_path: Path) -> None:
    """Defensive: sanity-check that target paths are the scoped ones only."""
    paths = _paths(tmp_path)
    _seed(paths)

    # Create a sibling ~/.claude-like dir and verify it survives.
    sibling = tmp_path / "dot_claude"
    sibling.mkdir()
    (sibling / "keep_me.txt").write_text("don't delete")

    cleanup.execute(paths, assume_yes=True)

    assert sibling.exists()
    assert (sibling / "keep_me.txt").read_text() == "don't delete"
