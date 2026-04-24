# claude-rotate

[![CI](https://github.com/codename-cn/claude-rotate/actions/workflows/ci.yml/badge.svg)](https://github.com/codename-cn/claude-rotate/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Mypy](https://img.shields.io/badge/type--checked-mypy%20strict-blue.svg)](http://mypy-lang.org/)

**Rotate multiple Anthropic [Claude Code](https://claude.com/claude-code) (Max / Pro) subscriptions from a single terminal.** `claude-rotate` probes each logged-in account's live 5-hour and weekly quota, picks the one with the most headroom, writes a full-scope `~/.claude/.credentials.json`, and `exec`s the real `claude` binary — no per-account shadow directories, no daily re-login, every Claude Code feature (Remote Control, session-scope commands, …) works.

```text
                   5h                         weekly                         expires
> Max-20 work      ██░░░░░░░░░░  19%  2h 42m  ████░░░░░░░░  34%   5d 9h 15m      22d
  Max-20 personal  ██████░░░░░░  51%  1h 12m  ████████░░░░  67%  2d 14h 30m       9d
  Max-5 alt        ███████████░  88%     25m  ███████████░  92%     18h 45m       4d
```

Built for developers and AI agents that burn through a single Max plan before lunch and want to keep working against Claude Code without hitting the 5-hour wall.

> **Keywords:** claude code rotator · multi-account claude · claude max rotation · anthropic oauth · claude rate-limit workaround · claude subscription manager · claude-code cli
>
> **Also searched as:** claude account switcher · claude 5-hour limit · claude weekly quota tool · claude max rate limit · claude-code multi-login · claude account manager · claude quota tracker · anthropic multi-account cli · claude pro pooling · rotate claude subscriptions

## 🤖 LLM Quickstart

1. Direct your favorite coding agent (Claude Code, Cursor, Aider, Codex, …) to [AGENTS.md](./AGENTS.md)
2. Prompt away!

## 👋 Human Quickstart

Requires Python ≥ 3.11 and [`claude`](https://claude.com/claude-code) (2.1.117+) on `PATH`.

**1. Install** from GitHub (not on PyPI yet):

```sh
uv tool install git+https://github.com/codename-cn/claude-rotate
# or: pipx install git+https://github.com/codename-cn/claude-rotate
```

**2. Wire up the shell alias** — scoped to `run` so only the rotation happens through the wrapper; `claude doctor`, `claude auth`, etc. still hit the real binary untouched:

```sh
# ~/.bashrc or ~/.zshrc
alias claude='claude-rotate run'
```

**3. Log in each subscription** (browser OAuth PKCE, one-time per account):

```sh
claude-rotate login work@example.com work
claude-rotate login personal@example.com personal
```

**4. Install the background sync** (recommended — keeps `accounts.json` in step with the tokens Claude Code rotates mid-session):

```sh
claude-rotate install-sync   # adds a */2 * * * * crontab entry
```

**5. Verify and go:**

```sh
claude-rotate doctor   # health check
claude-rotate status   # live quota dashboard

claude "explain this repo"   # picks the freshest account automatically
```

Each `login` opens a browser tab against `claude.com/cai/oauth/authorize`, runs the PKCE handshake, and captures the callback on a short-lived local port — no token pasting.

## Commands

| Command | What it does |
|---|---|
| `claude-rotate run [args…]` | Picks best account, exec `claude` (default when no command given) |
| `claude-rotate login <email> [<handle>]` | Add or re-login an account (interactive OAuth PKCE) |
| `claude-rotate login <email> <handle> --replace` | Overwrite an existing account |
| `claude-rotate list` | Show configured accounts (no network) |
| `claude-rotate status` | Live dashboard + health exit code |
| `claude-rotate status --json` | Machine-readable state |
| `claude-rotate pin <name>` / `unpin` | Force / resume rotation |
| `claude-rotate set-expiry <name> <value>` | Override subscription expiry (`YYYY-MM-DD`, `Nd`, or `""`) |
| `claude-rotate rename <old> <new>` | Rename an account handle |
| `claude-rotate remove <name>` | Delete an account (accepts handle or email) |
| `claude-rotate sync-credentials` | Reconcile `~/.claude/.credentials.json` → `accounts.json` (cron entry point) |
| `claude-rotate install-sync` / `--uninstall` | Install / remove the 2-minute sync crontab entry |
| `claude-rotate cleanup [--yes]` | Delete all rotate state (accounts, cache, logs) |
| `claude-rotate doctor` | Self-check (binary, config, tokens, refresh-token staleness) |

`<name>` accepts either the handle (`work`) or the account's email (`work@example.com`, case-insensitive).

## How it works

On every `claude-rotate run`:

1. **Pre-run reconcile** — read `~/.claude/.credentials.json` and sync any in-session token rotation back into `accounts.json` before picking.
2. **Pick** the account with the most 5-hour and weekly quota headroom (or the pinned account, if any).
3. **Refresh the access token** if it's older than 4 hours, using the OAuth refresh token stored in `accounts.json`.
4. **Write `~/.claude/.credentials.json`** with full scopes (`user:profile`, `user:inference`, `user:sessions:claude_code`, `user:mcp_servers`, `user:file_upload`) — the same shape Claude Code's own `/login` produces.
5. **`exec claude`** with `CLAUDE_CODE_OAUTH_TOKEN` stripped from the child environment. Claude Code reads the credentials file exactly as it would after a manual `/login`; every session-scope feature (Remote Control, `/ultrareview`, …) works.

The installed sync cron runs every 2 minutes and catches any drift between the two files while a long `claude` session is running: Anthropic rotates refresh tokens on each in-session refresh, and the cron keeps `accounts.json` authoritative so the next `run` never starts with a stale token.

## Why this exists

A single Max-plan session hits the 5-hour quota cap long before the day is over. Claude Code's default `/login` binds a single `~/.claude/.credentials.json` to one account, so round-robining across multiple subscriptions requires swapping credentials by hand — then re-authenticating every ~8 hours when the access token expires.

`claude-rotate` keeps the same on-disk contract that Claude Code expects (the OAuth-PKCE `.credentials.json`), but it *writes that file per run* from a multi-account store and refreshes tokens proactively. Rotation becomes invisible: the `claude` child sees a normal signed-in session, quotas and Max-plan metadata track correctly, and nothing downstream knows there are multiple accounts underneath.

## Platform support

| Platform | Status |
|---|---|
| Linux (any distro, Python 3.11+) | ✅ |
| macOS | ✅ |
| WSL2 | ✅ |
| Windows native | ❌ |

## How tokens are stored

Two files cooperate:

- **`~/.config/claude-rotate/accounts.json`** (Linux) / `~/Library/Application Support/claude-rotate/accounts.json` (macOS) — the multi-account store, `chmod 600`, parent dir `chmod 700`. Override the base directory with `CLAUDE_ROTATE_DIR=<path>`.
- **`~/.claude/.credentials.json`** — the single-account file Claude Code reads at startup. Written fresh by each `claude-rotate run` from the chosen account's tokens in `accounts.json`, `chmod 600`. The previous contents are snapshot-backed up alongside and pruned after 7 days.

The sync cron (`claude-rotate install-sync`) reconciles these two whenever Claude Code refreshes its own tokens mid-session, so `accounts.json` never falls out of date.

See [`SECURITY.md`](./SECURITY.md) for the threat model and redaction policy.

## Known quirks

### The `expires` column shows the next billing anchor, not a real cancellation

Anthropic's `/oauth/profile` endpoint does not surface pending cancellations — a subscription that has been cancelled on claude.ai stays `subscription_status: active` until the period actually ends. Without the cancel date available via API, the dashboard falls back to the next billing-anchor date, which for an active subscription is the *renewal* day, not a real end date. If you have scheduled a cancellation and want the real end date, fill it in during `claude-rotate login` (prompted after the handle) or set it later with `claude-rotate set-expiry <name> YYYY-MM-DD`.

### Refresh tokens expire after ~2 weeks of non-use

Anthropic invalidates OAuth refresh tokens after roughly two weeks of "stale idle." `claude-rotate doctor` warns when an account hasn't been touched for more than 10 days. If it does expire, just re-run `claude-rotate login <email> <handle> --replace`.

## Contributing

Issues and PRs welcome — see [`AGENTS.md`](./AGENTS.md) if you want your coding agent to help and [`SECURITY.md`](./SECURITY.md) for the redaction rules before pasting logs.

---

> ⭐ **If you find this useful, [star the repo](https://github.com/codename-cn/claude-rotate)** — it helps other devs with multiple Claude Code subscriptions find it.

## License

MIT — see [`LICENSE`](./LICENSE).
