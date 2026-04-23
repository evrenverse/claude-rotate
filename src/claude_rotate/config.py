"""Path resolution for claude-rotate.

Single source of truth for where accounts, caches, and logs live on disk.
Respects CLAUDE_ROTATE_DIR as a monolithic override, falls back to platformdirs
otherwise (which uses XDG on Linux and ~/Library on macOS).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_path, user_config_path, user_state_path

APP_NAME = "claude-rotate"

# Probe / selection constants (defaults; overridable via future config.json)
PROBE_MODEL = "claude-haiku-4-5-20251001"
PROBE_TIMEOUT_SECONDS = 12
PROBE_COOLDOWN_SECONDS = 300
# An account is considered ``usable`` as long as its utilisation is
# strictly less than this percentage. 100 means "burn every account to
# the real 429 wall before rotating away" — the deliberate default,
# because Anthropic's limit is 100%, not 95%. Lower values leave a
# defensive buffer at the cost of unused quota.
HEADROOM_PERCENT = 100.0
EXPIRY_URGENT_DAYS = 3
EXPIRY_SOON_DAYS = 10
SOON_QUOTA_CEILING_PERCENT = 70.0
BALANCE_THRESHOLD_PERCENT = 30.0
WEEKLY_WEIGHT = 3.0
HOURLY_WEIGHT = 1.0
METADATA_REFRESH_DAYS = 7
STALE_METADATA_WARN_DAYS = 10

# HTTP
# Legacy inference URL — no longer used by the probe layer (replaced by USAGE_URL)
# but kept here so any external code that imported it doesn't break immediately.
INFERENCE_URL = "https://api.anthropic.com/v1/messages"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# User-Agent for api.anthropic.com endpoints (/oauth/usage, /oauth/profile).
# These sit behind Cloudflare which rejects unfamiliar UAs with error 1010 —
# masquerading as Claude Code itself is safe here and was the approach the
# old Bash rotator used successfully.
USER_AGENT = "claude-code/2.1.117"
# Separate UA for platform.claude.com/v1/oauth/token. Anthropic's
# application-layer rate-limiter singles out the `claude-code/…` UA on this
# endpoint specifically (verified empirically: same request body, same client,
# only the UA differs → ``claude-code/2.1.117`` → HTTP 429 rate_limit, ``node``
# → HTTP 400 invalid_grant i.e. the endpoint actually processes our request).
# Using node-fetch's idiomatic UA bypasses that rule cleanly.
TOKEN_USER_AGENT = "node"
ANTHROPIC_BETA = "oauth-2025-04-20"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    cache_dir: Path
    state_dir: Path

    @property
    def accounts_file(self) -> Path:
        return self.config_dir / "accounts.json"

    @property
    def lock_file(self) -> Path:
        return self.config_dir / "accounts.json.lock"

    @property
    def usage_dir(self) -> Path:
        return self.cache_dir / "usage"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "log.jsonl"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def current_session_file(self) -> Path:
        return self.state_dir / "current-session.json"


def paths() -> Paths:
    override = os.environ.get("CLAUDE_ROTATE_DIR")
    if override:
        root = Path(override).expanduser()
        return Paths(
            config_dir=root / "config",
            cache_dir=root / "cache",
            state_dir=root / "state",
        )
    return Paths(
        config_dir=user_config_path(APP_NAME),
        cache_dir=user_cache_path(APP_NAME),
        state_dir=user_state_path(APP_NAME),
    )


def ensure_dirs(p: Paths) -> None:
    """Create all required directories with appropriate modes.

    Config dir is chmod 0700 because it holds tokens. Cache and state dirs
    are 0755 (default) — they never hold secrets.
    """
    p.config_dir.mkdir(parents=True, exist_ok=True)
    p.config_dir.chmod(0o700)
    p.cache_dir.mkdir(parents=True, exist_ok=True)
    p.usage_dir.mkdir(parents=True, exist_ok=True)
    p.state_dir.mkdir(parents=True, exist_ok=True)
