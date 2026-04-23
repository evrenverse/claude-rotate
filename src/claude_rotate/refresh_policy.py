"""When to pre-refresh an account's access token.

Pure function, no IO. The orchestrator (refresh.py) calls this before
spending an HTTP round trip on oauth.refresh_access_token.

Policy
------
- CI-path accounts (refresh_token is None) are never refreshed — the
  setup-token flow doesn't produce one.
- Accounts with an unknown runtime_token age (legacy v7 import, missing
  obtained_at) are refreshed unconditionally — we don't want to gamble
  on stale bits.
- Accounts with a known runtime_token age are refreshed when older than
  REFRESH_THRESHOLD.

The threshold is 4 hours: the token TTL is 8h, so ≤4h of age gives us
≥4h of safe runtime before `claude` itself has to refresh. Lower values
would spend an HTTP round trip on nearly every `run`; higher values
increase the odds that a long-running session started near expiry has
to refresh under load.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from claude_rotate.accounts import Account

REFRESH_THRESHOLD = timedelta(hours=4)


def should_refresh(account: Account, *, now: datetime) -> bool:
    if account.refresh_token is None:
        return False
    if account.runtime_token_obtained_at is None:
        return True
    age = now - account.runtime_token_obtained_at
    return age >= REFRESH_THRESHOLD
