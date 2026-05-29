"""Reconcile ~/.claude/.credentials.json with accounts.json.

Claude Code refreshes its own access token (and rotates the refresh
token) during long sessions. The refreshed material lives only in
.credentials.json until we sync it back to accounts.json. Without the
sync, the next `claude-rotate run` would use the stale refresh token
and be rejected by Anthropic, forcing the user to re-login.

This module is pure + testable. Two call sites:
  - claude-rotate sync-credentials (invoked by cron every 2 minutes)
  - claude-rotate run (pre-run reconcile, synchronously)

Who owns whom
-------------
accounts.json          — canonical source of truth for tokens at rest.
.credentials.json      — what claude sees at startup; claude writes
                         back on refresh.
current-session.json   — small breadcrumb written by `run` before
                         execvpe, tells the reconciler which account
                         owns the tokens currently in .credentials.json.
                         Without this we cannot distinguish `sub1` from
                         `sub2` after both have rotated past any direct
                         token match.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import urllib.error
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from claude_rotate.accounts import Store
from claude_rotate.config import Paths
from claude_rotate.credentials_file import CredentialsPayload, read_credentials, write_credentials
from claude_rotate.errors import ClaudeRotateError
from claude_rotate.oauth import refresh_access_token
from claude_rotate.refresh_policy import should_refresh


@dataclass(frozen=True)
class CurrentSession:
    account_name: str


def write_current_session(paths: Paths, session: CurrentSession) -> None:
    """Write current-session.json atomically. Mode 0o600 (tokens nearby)."""
    path = paths.current_session_file
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-session-")
    tmp = Path(tmp_str)
    try:
        tmp.chmod(0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"account_name": session.account_name}, f)
            f.write("\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_current_session(paths: Paths) -> CurrentSession | None:
    path = paths.current_session_file
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        name = raw.get("account_name")
        if not isinstance(name, str) or not name:
            return None
        return CurrentSession(account_name=name)
    except (OSError, json.JSONDecodeError):
        return None


def reconcile_once(
    payload: CredentialsPayload,
    paths: Paths,
    *,
    now: datetime,
) -> bool:
    """Update accounts.json with any token drift from .credentials.json.

    Returns True if accounts.json was modified, False otherwise.
    Silent on errors (missing session file, account no longer exists).
    """
    session = read_current_session(paths)
    if session is None:
        return False

    store = Store(paths)
    all_accounts = store.load()
    stored = all_accounts.get(session.account_name)
    if stored is None:
        return False

    access_changed = payload.access_token != stored.runtime_token
    refresh_changed = payload.refresh_token != stored.refresh_token
    if not access_changed and not refresh_changed:
        return False

    updated = replace(
        stored,
        runtime_token=payload.access_token,
        refresh_token=payload.refresh_token,
        runtime_token_obtained_at=now if access_changed else stored.runtime_token_obtained_at,
        refresh_token_obtained_at=now if refresh_changed else stored.refresh_token_obtained_at,
    )
    all_accounts[session.account_name] = updated
    store.save(all_accounts)
    return True


def reconcile_all(paths: Paths, *, now: datetime) -> bool:
    """Read .credentials.json and apply reconcile_once. Safe to call often."""
    payload = read_credentials()
    if payload is None:
        return False
    return reconcile_once(payload, paths, now=now)


def reconcile_isolated(paths: Paths, *, now: datetime) -> list[str]:
    """Sync each per-account isolated .credentials.json back to accounts.json.

    When session_isolation is on there is no single global credentials file;
    each account owns ``<configs>/<account>/.credentials.json``. A running
    session rotates tokens inside its own dir, so we read every account dir
    and write any drift back. Returns the names that changed.
    """
    from claude_rotate.credentials_file import CredentialsFile

    base = paths.account_configs_dir
    if not base.is_dir():
        return []
    store = Store(paths)
    accounts = store.load()
    changed: list[str] = []
    for name, acct in list(accounts.items()):
        cred_path = base / name / ".credentials.json"
        if not cred_path.exists():
            continue
        # Only adopt the file's tokens if it was written AFTER our last known
        # refresh. A file older than accounts.json is a stale leftover (an old
        # run wrote it, then the cron/ensure_fresh rotated accounts.json past
        # it); copying it back would revive an already-rotated, dead refresh
        # token — the bug behind the constant relogins. A genuine in-session
        # rotation by claude always post-dates obtained_at, so this still
        # captures those. (obtained_at None → legacy import, can't compare → adopt.)
        if acct.runtime_token_obtained_at is not None:
            file_mtime = datetime.fromtimestamp(cred_path.stat().st_mtime, UTC)
            if file_mtime <= acct.runtime_token_obtained_at:
                continue
        payload = CredentialsFile(base / name).read()
        if payload is None:
            continue
        access_changed = payload.access_token != acct.runtime_token
        refresh_changed = payload.refresh_token != acct.refresh_token
        if not access_changed and not refresh_changed:
            continue
        accounts[name] = replace(
            acct,
            runtime_token=payload.access_token,
            refresh_token=payload.refresh_token,
            runtime_token_obtained_at=now if access_changed else acct.runtime_token_obtained_at,
            refresh_token_obtained_at=now if refresh_changed else acct.refresh_token_obtained_at,
        )
        changed.append(name)
    if changed:
        store.save(accounts)
    return changed


def refresh_stale_tokens(paths: Paths, *, now: datetime, isolated: bool = False) -> list[str]:
    """Proactively refresh any OAuth account whose access token is stale.

    Without this, the rotator only refreshes the account a user actively
    picks via `run`. That leaves other accounts' tokens to expire silently
    during idle periods (PC closed, weekend off, etc.); the next `run`
    against them fails at probe time and may force a re-login.

    Called from the 2-minute cron job so tokens stay warm independent of
    whether a `claude` session is running. Errors are swallowed per
    account — one dead refresh token does not block the others.

    If the currently-active session (per current-session.json) was refreshed,
    ~/.claude/.credentials.json is also rewritten so the running claude
    and the next pre-run reconcile both see the new tokens.

    Returns the list of account names that were refreshed.
    """
    from claude_rotate.exec import build_credentials_payload  # local import to avoid cycle

    store = Store(paths)
    names = list(store.load().keys())
    refreshed: list[str] = []

    for name in names:
        # Each account's refresh is its own locked read-modify-write: re-load
        # and re-check staleness under the flock, so a concurrent writer (a
        # parallel run, the cron) can't make us spend a refresh token it has
        # already rotated — that double-spend trips reuse detection and kills
        # the family. LockTimeoutError ⊂ ClaudeRotateError → skip this tick.
        try:
            with store.locked() as locked:
                accounts = locked.load()
                acct = accounts.get(name)
                if acct is None or not should_refresh(acct, now=now):
                    continue
                assert acct.refresh_token is not None  # should_refresh guards this
                pair = refresh_access_token(acct.refresh_token)
                accounts[name] = replace(
                    acct,
                    runtime_token=pair.access_token,
                    refresh_token=pair.refresh_token,
                    runtime_token_obtained_at=now,
                    refresh_token_obtained_at=now,
                )
                locked.save(accounts)
        except (ClaudeRotateError, urllib.error.URLError, OSError):
            continue
        refreshed.append(name)

    if not refreshed:
        return []

    final = store.load()

    if isolated:
        # Isolation mode: push each refreshed account's fresh token into its own
        # configs/<name>/.credentials.json (when that dir exists). A running
        # isolated session re-reads its credentials file every turn, so it picks
        # the fresh token up, and the next run starts fresh too. Never write the
        # global file in this mode.
        base = paths.account_configs_dir
        for name in refreshed:
            cfg_dir = base / name
            if cfg_dir.is_dir():
                with contextlib.suppress(OSError):
                    write_credentials(
                        build_credentials_payload(final[name], now=now),
                        config_dir=cfg_dir,
                    )
    else:
        # Keep ~/.claude/.credentials.json in lockstep with the current session
        # so a running claude (or the next pre-run reconcile) sees the fresh
        # tokens and doesn't roll them back with the now-stale copy on disk.
        session = read_current_session(paths)
        if session and session.account_name in refreshed:
            active = final[session.account_name]
            # best-effort; accounts.json is still the source of truth
            with contextlib.suppress(OSError):
                write_credentials(build_credentials_payload(active, now=now))

    return refreshed


def mirror_session_to_global(paths: Paths, *, now: datetime) -> str | None:
    """Mirror the current session's access token into ~/.claude/.credentials.json.

    Isolation mode writes credentials only into per-account config dirs and points
    each live session's CLAUDE_CONFIG_DIR at its own dir. That leaves the default
    ~/.claude/.credentials.json frozen and expiring — which breaks any *headless*
    consumer that never sets CLAUDE_CONFIG_DIR and therefore falls back to the
    default dir (cron scripts, CI, the enniflow worker spawning `claude`). This
    keeps that fallback file fresh, mirroring whichever account ``run`` last
    activated (per current-session.json).

    The refresh token is deliberately stripped: in isolation mode nothing
    reconciles the global file back into accounts.json, so a headless `claude`
    that rotated it would orphan the new pair and make the next rotator refresh
    double-spend a dead refresh token (reuse detection kills the whole family).
    Without a refresh token the worst case is a clean auth failure once the
    access token finally expires — never a killed account. The cron's 2-minute
    cadence (and the ≤4h refresh threshold) keeps the mirrored access token well
    inside its 8h TTL, so headless callers effectively always see a live token.

    Idempotent: skips the write when the global file already holds this access
    token, so the 2-minute cron does not rewrite the global file every tick.

    Returns the mirrored account name when the file was (re)written, else None.
    """
    from claude_rotate.exec import build_credentials_payload  # local import to avoid cycle

    session = read_current_session(paths)
    if session is None:
        return None
    active = Store(paths).load().get(session.account_name)
    if active is None:
        return None

    existing = read_credentials()
    if existing is not None and existing.access_token == active.runtime_token:
        return None

    payload = replace(build_credentials_payload(active, now=now), refresh_token=None)
    try:
        write_credentials(payload)
    except OSError:
        return None
    return session.account_name
