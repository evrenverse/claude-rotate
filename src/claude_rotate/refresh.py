"""Pre-exec token-refresh orchestrator.

Given an Account, decide (via refresh_policy) whether to call
oauth.refresh_access_token, and if so, persist the rotated tokens back
into accounts.json. Returns the fresh Account so the caller can hand it
straight to exec.

Failures (HTTP 4xx/5xx, network) are swallowed — the caller gets back
the original Account and proceeds to exec. The child `claude` will then
show its own login prompt, which is more useful than a rotator-side
traceback.
"""

from __future__ import annotations

import urllib.error
from dataclasses import replace
from datetime import UTC, datetime

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths
from claude_rotate.errors import ClaudeRotateError
from claude_rotate.oauth import refresh_access_token
from claude_rotate.refresh_policy import should_refresh


def ensure_fresh(account: Account, paths: Paths, *, now: datetime | None = None) -> Account:
    """Return an Account whose runtime_token is fresh (or as fresh as we can make it).

    Any failure during the refresh call — HTTP 4xx/5xx from Anthropic, a
    transient network/DNS/TLS error, or a socket timeout — is swallowed.
    We return the original Account so the caller can still exec; claude
    itself will surface a login prompt if the stale token is rejected
    downstream, which is a more actionable failure than a rotator traceback.
    """
    if now is None:
        now = datetime.now(UTC)

    if not should_refresh(account, now=now):
        return account

    store = Store(paths)
    try:
        with store.locked() as locked:
            all_accounts = locked.load()
            stored = all_accounts.get(account.name, account)
            # Re-check under the lock against the freshest stored token: a
            # concurrent refresher (cron tick, parallel run) may have rotated
            # it while we waited for the lock. Spending the now-stale refresh
            # token again would trip reuse detection and revoke the family.
            if not should_refresh(stored, now=now):
                return stored
            assert stored.refresh_token is not None  # should_refresh guards this
            pair = refresh_access_token(stored.refresh_token)
            updated = replace(
                stored,
                runtime_token=pair.access_token,
                refresh_token=pair.refresh_token,
                runtime_token_obtained_at=now,
                refresh_token_obtained_at=now,
            )
            all_accounts[account.name] = updated
            locked.save(all_accounts)
            return updated
    except (ClaudeRotateError, urllib.error.URLError, OSError):
        # Swallow (incl. LockTimeoutError ⊂ ClaudeRotateError). exec proceeds
        # with the stale token; claude prompts for login if it's dead on arrival.
        return account
