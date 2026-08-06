# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] - 2026-08-06

### Added

- **Model-scoped weekly limits (e.g. Fable's own cap) in the status views.**
  The OAuth usage endpoint's `limits` array carries `weekly_scoped` windows
  the legacy top-level buckets don't. They render as dim-labelled full window
  lines beneath `week` in the dashboard and `status --report`, and appear in
  `--json`.

### Fixed

- **Concurrent writers no longer roll `accounts.json` back to spent tokens.**
  `Store.locked()` exists so a `load → modify → save` cycle runs as one
  critical section, but three automatic writers skipped it and saved a whole
  account map built from a stale read: `metadata.refresh_stale_accounts`
  (runs on every `run` and `status`, with multi-second HTTP probes inside the
  window) and `sync.reconcile_once` / `reconcile_isolated` (every 2-minute
  cron tick). A token rotated by the cron mid-window was overwritten with the
  pre-rotation value, so the next refresh re-sent an already-spent refresh
  token — tripping Anthropic's reuse detection, revoking the token family and
  forcing a relogin. All three now hold the lock; the metadata refresh keeps
  its probes *outside* the lock and merges only metadata fields against a
  freshly loaded store, keyed by name **and** `created_at` so a handle re-used
  by a different account does not inherit the old one's identity.
- **A corrupt `.credentials.json` no longer kills the sync cron.**
  `CredentialsFile.read()` raised on a half-written or schema-drifted file.
  Since `sync-credentials` reconciles *before* its proactive token refresh,
  the tick aborted and every account silently stopped being refreshed until
  the next relogin. The file now reads as "nothing to sync" instead, and
  `CredentialsPayload.from_json` rejects a non-object document cleanly.
- **Account names from CLI arguments are validated.** Names are used verbatim
  as path components (`usage/<name>.json`, `configs/<name>`), but only the
  interactive prompt checked them — a name passed as an argument to `login` or
  `rename` reached the store unchecked. Validation moved to a single
  `accounts.validate_account_name`, which both login paths reach through
  `build_account`. It also closes two holes in the old prompt-only regex: `.`
  and `..` matched its character class, and Python's `$` matches before a
  trailing newline, so `"..\n"` slipped past an anchored `re.match`.
- **Sub-1% usage no longer forecasts as a flat 0%.** `compute_forecast`
  truncated `pct` *before* projecting, so 0.7% burned over half a window
  projected to 0 instead of ~1%. Rounding now happens on the result.
- **Cache-backfilled model-scoped limits no longer masquerade as fresh data.**
  When the OAuth usage fetch fails (it rate-limits aggressively), the scoped
  weekly lines (e.g. Fable) are backfilled from the usage cache — previously
  with no indication, so a value could be hours old while the real limit was
  already reached. Cache entries now store the value's `fetched_at`; backfilled
  values are marked stale and render with the existing `~` cache marker in the
  dashboard and `status --report` (legend updated), and `--json` gains
  per-scoped-entry `stale` / `age_secs` fields. Legacy 3-element cache entries
  load as stale with unknown age.
- **`status` now persists successful scoped fetches.** Only `run` saved the
  usage cache, so a scoped value fetched live during `status` was displayed
  and thrown away — the cache (and every later backfill) stayed at whatever
  the last `run` happened to catch, hours or days old. `status` now writes
  fresh scoped limits back via `UsageCache.update_scoped` (only `w7_scoped`;
  `probed_at` and the burn-rate history stay untouched).

## [0.6.0] - 2026-06-20

Documented retroactively — this release shipped without changelog entries.

### Added

- **Recency-weighted forecast.** The projection assumed the window-average
  burn rate continued to reset, so a late burst was diluted across the whole
  elapsed window and under-projected. A per-account usage-history trail is now
  persisted (written on each launch, pruned to 12h / 500 points) and a recent
  tail rate derived from it; `compute_forecast` / `compute_limit_eta` blend
  that tail rate with the window average (`FORECAST_RECENCY_WEIGHT`, default
  0.6). With no history the average-pace path is unchanged. Routing keeps the
  stable average pace deliberately — the tail rate feeds only the rendered
  forecast.
- **Forecast horizon capped at subscription expiry.** When a subscription ends
  before the window resets, the projection is capped at the expiry and marked
  with a `⌛` in the dashboard and `status --report`; `status --json` exports
  the capped values.
- **Capacity-gated expiry selection.** A soon-expiring account keeps its
  Tier-1 / Tier-2 expiry shortcut only while it can still host another session
  in the current 5h window (`CAPACITY_GATE_THRESHOLD`); below that, the
  load- and pace-aware Tier-3 decides instead. It never makes an account
  unpickable.
- **Bordered compact dashboard.** Below the cards-mode width threshold the
  dashboard dropped its border and printed borderless text blocks.
  Phone-width terminals now get the same chrome as the wide table: a
  single-column rounded table with a rule between accounts. Greying parity is
  kept — unusable accounts flatten to grey, loud error labels stay coloured.

### Fixed

- **Tier-3 no longer picks an account it projects to blow past its limit.**
  A high weekly urgency (earliest weekly reset) could keep winning Tier-3 even
  while the account's 5h or weekly forecast projected >100% and another
  account sat fresh — the soft `[0,1]` dampeners cannot overcome the unbounded
  weekly urgency. A hard gate now yields to any account that is neither
  capacity-gated nor forecast over 100%; only when every usable account is
  gated does the drain score rank the whole set. The gate reuses the same
  `compute_forecast` the dashboard renders, so what the user sees drives the
  pick.

## [0.5.0] - 2026-06-14

### Added

- **Load-aware account distribution via live-session tracking.** A burst of
  concurrent `claude-rotate run` invocations now fans out across accounts
  *before* the usage probe can see the load: each run records its live session
  in a PID-based registry (`state/sessions/`), and the selection heuristic
  feeds the per-account session count into its tier-3 drain score as a
  multiplicative dampener, so simultaneously-launched sessions spread out
  instead of stampeding one account. Dead sessions are reaped lazily via
  process liveness (`psutil`), and a dedicated lock serialises concurrent
  picks. New `claude-rotate install-hooks` registers a heartbeat hook in
  `~/.claude/settings.json` for precise active/idle classification; `status`
  (and `--report` / `--json`) shows a per-account live-session count
  (`N active · M idle`). On by default; disable with
  `claude-rotate config set session_tracking false`.
- **Live `status --watch` mode.** `claude-rotate status --watch [SECONDS]`
  runs a live view on the alternate screen, re-probing and redrawing every
  `SECONDS` (default 5, floored at 1). The slow probe runs while the previous
  frame stays on screen, so each refresh is flicker-free; a footer shows the
  local refresh time, the cadence, and `Ctrl-C` to quit (which exits cleanly
  and restores the terminal). Only engages on a real terminal — piped or
  captured runs fall through to a single render so `--json` / `--report`
  output stays clean.

## [0.4.0] - 2026-06-13

### Added

- **`claude-rotate disable <name>` / `enable <name>`.** Manually take an
  account out of rotation without removing it. A disabled account is never
  auto-picked — not even as a last-resort fallback when every other account is
  exhausted (`run` refuses to launch and points you at `enable` when *all*
  accounts are disabled) — yet it is still probed and shown so you keep the
  full picture: the dashboard, `list`, and `--report` render it greyed-out
  with a `disabled` hint and a `⊘` marker. Disabling is reversible and may
  apply to any number of accounts at once; disabling a pinned account clears
  the pin, and pinning a disabled account clears the disable (the two are
  mutually exclusive). `status --json` gains a per-account `disabled` boolean.
- **`status` table sorted by subscription expiry.** The dashboard now always
  orders accounts so the earliest-expiring subscription is at the top
  (accounts with no known expiry last), making the most urgent-to-use
  subscription immediately visible.

### Changed

- **Responsive `status` dashboard.** The quota dashboard now adapts to the
  terminal width: gradient bars grow from 8 to 20 cells with available space,
  relative durations are dropped first when space gets tight, and below ~76
  columns the table folds into one compact card per account. Each account
  spans two lines per window — a fact line (bar, usage %, absolute local
  reset clock with weekday slot + relative duration) and a dimmed forecast
  sub-line (projected % at reset and, when the limit is crossed before reset,
  the clock at which usage hits 100%, red when under an hour away). The
  dashboard also gained the `--report` view's context: a session status line,
  an `@` marker for the account this session runs on (alongside `>` next pick
  and `★` pinned), a plan badge per account, the subscription column with the
  absolute end date beneath the days left, and a risk footer with warnings
  plus the freest fallback account. Quota semantics (forecast, limit ETA,
  risk thresholds, wording) moved to a shared `insights` module used by both
  the dashboard and `--report`. `status --json` now also reports the active
  session account as a top-level `active` field.

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
