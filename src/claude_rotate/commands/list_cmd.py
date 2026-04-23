"""`claude-rotate list` — all accounts, no live probe."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from rich.console import Console
from rich.table import Table

from claude_rotate.accounts import Store
from claude_rotate.config import STALE_METADATA_WARN_DAYS, Paths
from claude_rotate.dashboard import fmt_sub_expiry


def execute(paths: Paths) -> int:
    accounts = Store(paths).load()
    console = Console(file=sys.stderr)

    console.print()
    if not accounts:
        console.print("  No accounts configured. Run: claude-rotate login <email> [name]")
        console.print()
        return 0

    now = datetime.now(UTC)
    console.print("  Configured accounts:")
    console.print()

    # Same grid style as the dashboard so the two outputs feel related.
    table = Table.grid(padding=(0, 2))
    table.add_column("marker", no_wrap=True)
    table.add_column("name", no_wrap=True)
    table.add_column("plan", no_wrap=True)
    table.add_column("email", no_wrap=True)
    table.add_column("expires", no_wrap=True)
    table.add_column("flags", no_wrap=True)

    for name, a in sorted(accounts.items()):
        # Pinned indicator — leading ★, same convention as dashboard's ">"
        marker = "[yellow]★[/]" if a.pinned else " "

        # Expiry column (reuses dashboard colour gradient)
        exp_text, exp_style = fmt_sub_expiry(
            a.effective_expires_at,
            status=a.subscription_status,
            now=now,
        )
        exp_cell = f"[{exp_style}]{exp_text}[/]" if exp_style and exp_text else exp_text

        # Stale warning — only for OAuth-logged-in accounts
        flags = ""
        if a.refresh_token is not None:
            last = a.metadata_refreshed_at
            age_days = None if last is None else (now - last).days
            if age_days is None or age_days > STALE_METADATA_WARN_DAYS:
                age_label = f"{age_days}d" if age_days is not None else "never"
                flags = f"[yellow]⚠ stale {age_label}[/]"

        email = a.email or "[dim]<no email>[/]"

        table.add_row(marker, name, a.plan, email, exp_cell, flags)

    console.print(table)
    console.print()
    return 0
