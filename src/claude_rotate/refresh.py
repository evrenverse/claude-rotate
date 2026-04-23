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

    assert account.refresh_token is not None  # should_refresh guards this

    try:
        pair = refresh_access_token(account.refresh_token)
    except (ClaudeRotateError, urllib.error.URLError, OSError):
        # Swallow. exec will proceed with the stale token; claude will
        # prompt for login if the token turns out to be dead on arrival.
        return account

    updated = replace(
        account,
        runtime_token=pair.access_token,
        refresh_token=pair.refresh_token,
        runtime_token_obtained_at=now,
        refresh_token_obtained_at=now,
    )

    store = Store(paths)
    all_accounts = store.load()
    all_accounts[account.name] = updated
    store.save(all_accounts)

    return updated
