# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- **Global credentials mirror in isolation mode.** The sync cron no longer
  mirrors the most recently launched account's access token into the global
  `~/.claude/.credentials.json`. The mirror re-pointed *running* headless
  sessions (which re-read the credentials file every turn) at a different
  account whenever a new interactive session launched — invalidating their
  org-scoped prompt cache mid-run and re-billing the full context of every
  active session. In isolation mode the rotator now never writes the global
  file; headless consumers pin an account via
  `CLAUDE_CONFIG_DIR=~/.config/claude-rotate/configs/<account>`, which the
  cron keeps fresh.

## [0.3.0] - 2026-06-05

### Added

- **`claude-rotate status --report`** — a compact, single-table account
  overview complementing the rich dashboard. It marks the account this session
  runs on (`@`), the rotator's next pick (`>`), or both (`@>`); shows each
  account's 5-hour and weekly usage, resets (absolute clock + weekday when on
  another day + relative), and days left on the subscription; and surfaces
  warnings (weekly ≥ 90 %, forecast > 100 %, expiry < 7 days, re-login needed)
  plus the freest fallback account. Output is fenced as a Markdown code block
  when captured (e.g. by the skill) and clean when run in a terminal.
- **Bundled agent skill + `claude-rotate install-skill`.** Installs an
  `account` skill (a thin wrapper around `status --report`) so coding agents can
  report the active account and limits on demand. It is written once to the
  shared store `~/.agents/skills/account` and symlinked into every detected
  agent — Claude Code, Codex, Gemini, and opencode. `--uninstall` removes the
  symlinks and the canonical copy. The skill ships as package data.

## [0.2.0] - 2026-05-30

### Added

- **Quota forecast in the status dashboard.** Each 5-hour and weekly bar in
  `claude-rotate status` (and the dashboard shown while wrapping `claude`) now
  renders a linear projection `[→XX%]` of where that quota lands at window
  reset if the current burn rate holds — the same math as the companion Bash
  statusline. The projection is dropped once a window is already at/over 100%
  (it would only be noise) and capped at 999%. Disable it with
  `CLAUDE_ROTATE_FORECAST=0`. The same figures are exposed in
  `claude-rotate status --json` as `h5_forecast_pct` / `w7_forecast_pct`
  (always present, independent of the env toggle).

## [0.1.1] - 2026-05-26

### Fixed

- **Session isolation: accounts no longer get logged out repeatedly.** Two
  bugs in the isolation token-sync path could revoke an account's tokens
  server-side and force a relogin:
  - `reconcile_isolated` copied a per-account `.credentials.json` back into
    `accounts.json` with no recency check, so a stale leftover file could roll
    a freshly refreshed token back onto an already-rotated (dead) one. It now
    only adopts a file written *after* the stored token's `obtained_at`.
  - The `accounts.json` flock covered only the final write, so a cron tick and
    a `run` could each spend the same rotating refresh token and trip
    Anthropic's refresh-token-reuse detection (which revokes the whole family).
    Refresh now holds the lock across the entire load → refresh → save and
    re-checks staleness under it.

## [0.1.0]

- Initial public release.
