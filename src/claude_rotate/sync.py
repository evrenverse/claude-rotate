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
from datetime import datetime
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


def refresh_stale_accounts(paths: Paths, *, now: datetime) -> list[str]:
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
    accounts = store.load()
    refreshed: list[str] = []

    for name, acct in accounts.items():
        if not should_refresh(acct, now=now):
            continue
        assert acct.refresh_token is not None  # should_refresh guards this
        try:
            pair = refresh_access_token(acct.refresh_token)
        except (ClaudeRotateError, urllib.error.URLError, OSError):
            continue
        accounts[name] = replace(
            acct,
            runtime_token=pair.access_token,
            refresh_token=pair.refresh_token,
            runtime_token_obtained_at=now,
            refresh_token_obtained_at=now,
        )
        refreshed.append(name)

    if not refreshed:
        return []

    store.save(accounts)

    # Keep .credentials.json in lockstep with the current session so a
    # running claude (or the next pre-run reconcile) sees our fresh tokens
    # and doesn't roll them back with the now-stale copy on disk.
    session = read_current_session(paths)
    if session and session.account_name in refreshed:
        active = accounts[session.account_name]
        # best-effort; accounts.json is still the source of truth
        with contextlib.suppress(OSError):
            write_credentials(build_credentials_payload(active, now=now))

    return refreshed
