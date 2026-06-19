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
# The 5h burn-pace dampener needs a minimum observation period before its
# rate estimate means anything — 3% burned two minutes into a window
# projects >100% numerically but is pure noise (one short session or a
# probe burst). Below this elapsed time only the level dampener applies.
PACE_MIN_ELAPSED_SECONDS = 900
METADATA_REFRESH_DAYS = 7
STALE_METADATA_WARN_DAYS = 10
# Live-session tracking — feeds the load-aware selection dampener. A freshly
# launched session counts as "active" for this window even before any heartbeat
# fires (last_active is initialised to started_at); afterwards only heartbeats
# keep it active, by which time real usage shows up in the probe.
SESSION_ACTIVE_WINDOW_SECONDS = 90
# Weight of an idle (open but not recently active) session relative to an
# active one when summing per-account load.
SESSION_IDLE_WEIGHT = 0.3
# Strength of the per-load-unit penalty in the tier-3 drain score
# (session_load_availability = max(0, 1 - weighted_load * penalty)).
SESSION_LOAD_PENALTY = 0.25
# Expiry-tier capacity gate — a soon-expiring account keeps its expiry-priority
# shortcut (Tier-1 / Tier-2 soon-exception) only while it can still host another
# session in the current 5h window. Below this share of a fresh account's capacity
# the shortcut is skipped for that pick and the load/pace-aware Tier-3 decides
# instead. Never makes the account unpickable. Tunable; the sole knob for how
# eagerly draining yields to load-spreading.
CAPACITY_GATE_THRESHOLD = 0.5

# Forecast windows — used by the status dashboard's [→XX%] projection to
# derive elapsed time from seconds-until-reset. Same lengths the Bash
# statusline uses (5-hour and 7-day unified rate-limit windows).
FORECAST_WINDOW_5H_SECONDS = 5 * 3600  # 18000
FORECAST_WINDOW_7D_SECONDS = 7 * 86400  # 604800
# Weekly analogue of PACE_MIN_ELAPSED_SECONDS: ignore the weekly forecast until
# this much of the 7d window has elapsed, so a freshly reset window with a small
# early burst doesn't project a noisy >100%. Same 5% share as the 5h gate.
WEEKLY_PACE_MIN_ELAPSED_SECONDS = FORECAST_WINDOW_7D_SECONDS // 20  # ~8.4h

# HTTP
INFERENCE_URL = "https://api.anthropic.com/v1/messages"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# User-Agent for api.anthropic.com endpoints (/v1/messages, /oauth/profile).
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
    def account_configs_dir(self) -> Path:
        return self.config_dir / "configs"

    @property
    def current_session_file(self) -> Path:
        return self.state_dir / "current-session.json"

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def sessions_lock(self) -> Path:
        return self.state_dir / "sessions.lock"


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
