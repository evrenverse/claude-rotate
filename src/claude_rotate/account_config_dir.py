"""Build/refresh a per-account CLAUDE_CONFIG_DIR as a symlink mirror of ~/.claude.

Every entry of ~/.claude is symlinked into the per-account dir EXCEPT
.credentials.json and any ``.credentials.json.*`` siblings (tmp writes,
legacy backups): that one file is written real and per-account by exec.py,
so a running session reads its own token and a parallel run on another
account can never overwrite it. Everything else
(projects/, history, .claude.json, settings, plugins, …) stays shared, so
the user's dashboards, /resume and stats keep working unchanged.

Validated 2026-05-25 (Claude Code 2.1.150): Claude writes shared files
in-place through the symlink, so the links survive.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from claude_rotate.config import Paths

_CREDENTIALS_PREFIX = ".credentials.json"
_DIVERGED_PREFIX = ".diverged-"
_DIVERGED_MAX_AGE_DAYS = 7


def home_claude_dir() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))) / ".claude"


def _prune_diverged(target: Path, *, now: float) -> None:
    """Drop self-heal backups (``.diverged-<name>-<ts>``) older than the cutoff.

    Divergence is rare, but without this the backups would accumulate forever.
    Parses the trailing unix-ts segment to decide what is old enough to drop.
    """
    threshold = now - _DIVERGED_MAX_AGE_DAYS * 86400
    for p in target.glob(f"{_DIVERGED_PREFIX}*"):
        try:
            ts = int(p.name.rsplit("-", 1)[1])
        except (ValueError, IndexError):
            continue
        if ts < threshold:
            with contextlib.suppress(OSError):
                p.unlink()


def ensure_account_config_dir(paths: Paths, account_name: str, *, home_claude: Path) -> Path:
    target = paths.account_configs_dir / account_name
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)

    desired: dict[str, Path] = {}
    # If ~/.claude is absent (essentially only on a brand-new machine before
    # claude has ever run), desired stays empty: we prune any stale links and
    # leave the dir holding just the real .credentials.json. Intentional
    # graceful degrade, not a crash.
    if home_claude.is_dir():
        for entry in home_claude.iterdir():
            if entry.name.startswith(_CREDENTIALS_PREFIX):
                continue
            desired[entry.name] = entry

    # Prune stale/incorrect symlinks (never touch the real .credentials.json).
    for child in target.iterdir():
        if child.name.startswith(_CREDENTIALS_PREFIX):
            continue
        if child.is_symlink():
            points_at = child.readlink()
            if child.name not in desired or points_at != desired[child.name]:
                child.unlink()

    # Create missing symlinks; self-heal a real file/dir that replaced a link.
    for name, src in desired.items():
        link = target / name
        if link.is_symlink():
            continue
        if link.exists():
            # A real file/dir diverged from the shared mirror (e.g. an atomic
            # write replaced the symlink). The shared ~/.claude is the source of
            # truth: back the divergent copy up and re-link to the shared source.
            link.rename(target / f".diverged-{name}-{int(time.time())}")
        link.symlink_to(src)

    _prune_diverged(target, now=time.time())
    return target
