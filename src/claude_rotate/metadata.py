"""Background metadata refresh.

Every run checks whether any account's metadata is older than
METADATA_REFRESH_DAYS and, if so, probes the usage endpoint to confirm
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
from claude_rotate.probe import fetch_usage
from claude_rotate.state_log import StateLog


def refresh_stale_accounts(paths: Paths, *, now: datetime | None = None) -> None:
    """Lightweight refresh: probe each stale account; update metadata on success."""
    now = now or datetime.now(UTC)
    threshold = now - timedelta(days=METADATA_REFRESH_DAYS)
    store = Store(paths)
    accounts = store.load()
    log = StateLog(paths)
    updated = False
    for name, acct in accounts.items():
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

        accounts[name] = replace(acct, **updates)  # type: ignore[arg-type]
        updated = True
        log.event("metadata_refreshed", account=name)
    if updated:
        store.save(accounts)
