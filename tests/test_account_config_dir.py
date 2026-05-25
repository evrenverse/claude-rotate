from __future__ import annotations

from pathlib import Path

from claude_rotate.account_config_dir import ensure_account_config_dir
from claude_rotate.config import paths


def _fake_home_claude(root: Path) -> Path:
    hc = root / ".claude"
    (hc / "projects").mkdir(parents=True)
    (hc / "todos").mkdir()
    (hc / "settings.json").write_text("{}\n")
    (hc / ".claude.json").write_text("{}\n")
    (hc / ".credentials.json").write_text('{"claudeAiOauth": {}}\n')
    (hc / ".credentials.json.bak-123").write_text("{}\n")
    return hc


def test_mirror_links_everything_except_credentials(rotate_dir: Path, tmp_path: Path) -> None:
    hc = _fake_home_claude(tmp_path / "home")
    target = ensure_account_config_dir(paths(), "matri", home_claude=hc)

    assert target == paths().account_configs_dir / "matri"
    for name in ("projects", "todos", "settings.json", ".claude.json"):
        link = target / name
        assert link.is_symlink()
        assert link.readlink() == hc / name
    assert not (target / ".credentials.json").is_symlink()
    assert not (target / ".credentials.json.bak-123").exists()


def test_refresh_adds_new_and_prunes_dead(rotate_dir: Path, tmp_path: Path) -> None:
    hc = _fake_home_claude(tmp_path / "home")
    target = ensure_account_config_dir(paths(), "matri", home_claude=hc)

    (hc / "plugins").mkdir()
    (hc / "todos").rmdir()

    ensure_account_config_dir(paths(), "matri", home_claude=hc)
    assert (target / "plugins").is_symlink()
    assert not (target / "todos").is_symlink()  # pruned


def test_dir_is_0700(rotate_dir: Path, tmp_path: Path) -> None:
    hc = _fake_home_claude(tmp_path / "home")
    target = ensure_account_config_dir(paths(), "matri", home_claude=hc)
    assert (target.stat().st_mode & 0o777) == 0o700


def test_self_heals_diverged_real_file(rotate_dir: Path, tmp_path: Path) -> None:
    hc = _fake_home_claude(tmp_path / "home")
    target = ensure_account_config_dir(paths(), "matri", home_claude=hc)

    (target / "settings.json").unlink()
    (target / "settings.json").write_text('{"diverged": true}\n')

    ensure_account_config_dir(paths(), "matri", home_claude=hc)
    assert (target / "settings.json").is_symlink()
    assert (target / "settings.json").readlink() == hc / "settings.json"
    assert any(p.name.startswith(".diverged-settings.json-") for p in target.iterdir())
