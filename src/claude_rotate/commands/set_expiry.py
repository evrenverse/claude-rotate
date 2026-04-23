"""`claude-rotate set-expiry <name> <value>` — manual subscription-expiry override.

Anthropic's /oauth/profile does not surface pending cancellations (status
stays ``active`` until the period actually ends). This command lets the
user pin the real end date — typically copied from the
"Your subscription will be canceled on …" banner on claude.ai — so the
dashboard and selection logic stop showing the misleading next-billing
anchor and use the real end date instead.

Value formats (same as login prompt):
  ``YYYY-MM-DD``  absolute date (end-of-day UTC)
  ``Nd``          N days from now
  ``""``          clear the override (fall back to API-derived value)
"""

from __future__ import annotations

import sys
from dataclasses import replace

from claude_rotate.accounts import Store, resolve_name
from claude_rotate.config import Paths
from claude_rotate.login import parse_expiry


def execute(paths: Paths, name: str, value: str) -> int:
    store = Store(paths)
    accounts = store.load()
    resolved = resolve_name(accounts, name)
    if resolved is None:
        print(f"error: account {name!r} not found", file=sys.stderr)
        return 1
    name = resolved

    parsed = parse_expiry(value) if value else None
    accounts[name] = replace(accounts[name], subscription_expires_at_manual=parsed)
    store.save(accounts)

    if parsed is None:
        print(f"  ✓ Cleared manual expiry for {name}", file=sys.stderr)
    else:
        print(
            f"  ✓ Set manual expiry for {name}: {parsed.date().isoformat()}",
            file=sys.stderr,
        )
    return 0
