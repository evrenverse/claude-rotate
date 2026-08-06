"""Background metadata refresh.

Every run checks whether any account's metadata is older than
METADATA_REFRESH_DAYS and, if so, probes quota headers to confirm
the token still works. Failures are silently logged — the refresh must never
block the primary run path.

For accounts with a refresh_token, we also re-fetch the OAuth profile to
keep email, plan, subscription_status, and subscription_expires_at current.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from claude_rotate.accounts import Store
from claude_rotate.config import METADATA_REFRESH_DAYS, Paths
from claude_rotate.errors import LockTimeoutError
from claude_rotate.probe import fetch_usage
from claude_rotate.state_log import StateLog


def refresh_stale_accounts(paths: Paths, *, now: datetime | None = None) -> None:
    """Lightweight refresh: probe each stale account; update metadata on success.

    The probes run *outside* the accounts.json lock — they take seconds, and
    holding the lock across them would stall every concurrent writer. The
    collected updates are then merged under the lock against a freshly loaded
    store, so a token another process rotated while we were probing survives:
    we only ever write back the metadata fields computed here, never the token
    fields we read at the start.

    The merge is keyed by name *and* ``created_at``, because a name alone is
    not a stable identity — an account removed and re-added under the same
    handle while we were probing is a different account, and copying the old
    one's email/plan onto it would misattribute it.
    """
    now = now or datetime.now(UTC)
    threshold = now - timedelta(days=METADATA_REFRESH_DAYS)
    store = Store(paths)
    log = StateLog(paths)
    pending: dict[str, tuple[datetime, dict[str, object]]] = {}
    for name, acct in store.load().items():
        last = acct.metadata_refreshed_at
        if last is not None and last >= threshold:
            continue
        result = fetch_usage(acct.runtime_token)
        if not result.ok:
            log.event("metadata_refresh_probe_failed", account=name, error=result.error)
            continue

        # Start with just the timestamp update
        updates: dict[str, object] = {"metadata_refreshed_at": now}

        # If this account has a refresh_token, also re-fetch profile info
        if acct.refresh_token is not None:
            try:
                from claude_rotate.oauth import derive_subscription_expiry, fetch_profile

                profile = fetch_profile(acct.runtime_token)
                if profile.ok:
                    sub_expires = derive_subscription_expiry(
                        subscription_status=profile.subscription_status,
                        subscription_created_at=profile.subscription_created_at,
                        now=now,
                    )
                    if profile.email:
                        updates["email"] = profile.email
                    if profile.plan != "unknown":
                        updates["plan"] = profile.plan
                    updates["subscription_status"] = profile.subscription_status
                    updates["subscription_expires_at"] = sub_expires
            except Exception:
                pass  # profile refresh is best-effort; don't fail the usage refresh

        pending[name] = (acct.created_at, updates)

    if not pending:
        return

    try:
        with store.locked() as locked:
            accounts = locked.load()
            for name, (created_at, updates) in pending.items():
                current = accounts.get(name)
                if current is None or current.created_at != created_at:
                    continue  # removed, or re-added as a different account
                accounts[name] = replace(current, **updates)  # type: ignore[arg-type]
                log.event("metadata_refreshed", account=name)
            locked.save(accounts)
    except LockTimeoutError:
        # Metadata is cosmetic and the accounts stay stale-but-valid; the next
        # run retries. Never let this block the caller's primary work.
        log.event("metadata_refresh_lock_timeout")
