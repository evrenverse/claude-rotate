"""`claude-rotate run …` — the default command path."""

from __future__ import annotations

import contextlib
import sys
from dataclasses import replace
from datetime import UTC, datetime

from rich.console import Console

from claude_rotate.accounts import Store
from claude_rotate.config import Paths
from claude_rotate.dashboard import (
    DashboardRow,
    compact_one_liner,
    render_dashboard,
    render_stale_footer,
)
from claude_rotate.exec import exec_claude
from claude_rotate.metadata import refresh_stale_accounts
from claude_rotate.probe import ProbeResult, probe_many
from claude_rotate.refresh import ensure_fresh
from claude_rotate.selection import Candidate, is_usable, pick_best
from claude_rotate.state_log import StateLog
from claude_rotate.sync import reconcile_all
from claude_rotate.usage_cache import UsageCache


def execute(paths: Paths, claude_args: list[str]) -> int:
    # Best-effort; never blocks or raises
    with contextlib.suppress(Exception):
        refresh_stale_accounts(paths)

    # Pre-run reconcile: pull any drift the cron hasn't picked up yet.
    # Safe to call even when .credentials.json doesn't exist.
    with contextlib.suppress(Exception):
        reconcile_all(paths, now=datetime.now(UTC))

    store = Store(paths)
    accounts = store.load()
    if not accounts:
        _print_no_accounts_message()
        return 3

    # Probe every account so the dashboard is always complete — pinning
    # only constrains the selection pool, not what the user sees.
    pool = list(accounts.values())
    pinned_names = {a.name for a in pool if a.pinned}

    candidates = probe_many(pool)
    cache = UsageCache(paths)

    # Fill in any candidates that failed live-probe with cached values
    resolved: list[Candidate] = []
    dashboard_rows: list[DashboardRow] = []
    for c in candidates:
        if c.h5_pct is None and c.w7_pct is None:
            err = c.probe_error
            # Unauthorized → relogin row, skip from resolved
            if err == "unauthorized":
                dashboard_rows.append(
                    DashboardRow(
                        account=c.account,
                        h5_pct=None,
                        w7_pct=None,
                        h5_reset_secs=0,
                        w7_reset_secs=0,
                        status="relogin",
                        note="token invalid (401/403)",
                    )
                )
                continue
            # 429 on /oauth/usage is an API rate-limit on the *probe*
            # endpoint, not a subscription-quota exhaustion. The account
            # itself may still be fully usable. Fall back to cached usage
            # values and keep the candidate in the selection pool.
            if err == "rate_limited":
                cached = cache.load(c.account.name)
                if cached is not None:
                    c = replace(
                        c,
                        h5_pct=cached.h5_pct,
                        w7_pct=cached.w7_pct,
                        h5_reset_secs=cached.h5_reset_secs,
                        w7_reset_secs=cached.w7_reset_secs,
                    )
                    dashboard_rows.append(
                        DashboardRow(
                            account=c.account,
                            h5_pct=c.h5_pct,
                            w7_pct=c.w7_pct,
                            h5_reset_secs=c.h5_reset_secs,
                            w7_reset_secs=c.w7_reset_secs,
                            from_cache=True,
                        )
                    )
                    resolved.append(c)
                else:
                    dashboard_rows.append(
                        DashboardRow(
                            account=c.account,
                            h5_pct=None,
                            w7_pct=None,
                            h5_reset_secs=0,
                            w7_reset_secs=0,
                            status="no_data",
                            note="probe API rate-limited; no cached data",
                        )
                    )
                continue
            # Other errors — try cache fallback
            cached = cache.load(c.account.name)
            if cached is not None:
                c = replace(
                    c,
                    h5_pct=cached.h5_pct,
                    w7_pct=cached.w7_pct,
                    h5_reset_secs=cached.h5_reset_secs,
                    w7_reset_secs=cached.w7_reset_secs,
                )
                dashboard_rows.append(
                    DashboardRow(
                        account=c.account,
                        h5_pct=c.h5_pct,
                        w7_pct=c.w7_pct,
                        h5_reset_secs=c.h5_reset_secs,
                        w7_reset_secs=c.w7_reset_secs,
                        from_cache=True,
                    )
                )
            else:
                dashboard_rows.append(
                    DashboardRow(
                        account=c.account,
                        h5_pct=None,
                        w7_pct=None,
                        h5_reset_secs=0,
                        w7_reset_secs=0,
                        status="no_data",
                    )
                )
                continue
        else:
            cache.save(c.account.name, _to_probe_result(c))
            dashboard_rows.append(
                DashboardRow(
                    account=c.account,
                    h5_pct=c.h5_pct,
                    w7_pct=c.w7_pct,
                    h5_reset_secs=c.h5_reset_secs,
                    w7_reset_secs=c.w7_reset_secs,
                )
            )
        resolved.append(c)

    if not resolved:
        # No data anywhere → nothing to rotate. Exec first account with cached token.
        # Prefer pinned if one exists — honouring the user's intent even with no probe data.
        first = next(
            (a for a in accounts.values() if a.pinned),
            next(iter(accounts.values())),
        )
        StateLog(paths).event("exec", chosen=first.name, reason="no_probe_data")
        _emit_rows(dashboard_rows, chosen=first.name)
        fresh = ensure_fresh(first, paths)
        return exec_claude(fresh, paths, claude_args)

    # Pinning: restrict the selection pool to the pinned account(s) only.
    # Non-pinned accounts stay in dashboard_rows so the user still sees them.
    selection_pool = (
        [c for c in resolved if c.account.name in pinned_names] if pinned_names else resolved
    )
    if not selection_pool:
        # Pinned account(s) all failed live probe; fall back to whatever we have
        selection_pool = resolved

    best, wait_msg = pick_best(selection_pool)

    # pick_best renders "all accounts exhausted; X available in …" which
    # is a lie when the pool was trimmed by pinning — rotation would
    # rescue us if the user unpinned. Rewrite the message to say that.
    if wait_msg and pinned_names:
        has_alternative = any(c.account.name not in pinned_names and is_usable(c) for c in resolved)
        in_idx = wait_msg.find(" in ")
        remainder = wait_msg[in_idx:] if in_idx != -1 else ""
        wait_msg = f"pinned account {best.account.label!r} exhausted; available{remainder}"
        if has_alternative:
            wait_msg += " — run `claude-rotate unpin` to rotate instead"

    _emit_rows(dashboard_rows, chosen=best.account.name, wait_msg=wait_msg)
    StateLog(paths).event(
        "exec",
        chosen=best.account.name,
        h5_pct=best.h5_pct,
        w7_pct=best.w7_pct,
        wait=wait_msg,
    )
    fresh = ensure_fresh(best.account, paths)
    return exec_claude(fresh, paths, claude_args)


def _print_no_accounts_message() -> None:
    import glob
    import os

    home = os.environ.get("HOME", "")
    legacy = glob.glob(os.path.join(home, ".claude", ".credentials-*.json"))

    print("", file=sys.stderr)
    print("  No accounts configured.", file=sys.stderr)
    if legacy:
        print(
            "\n  I noticed legacy ~/.claude/.credentials-*.json files — those are\n"
            "  short-lived browser-OAuth tokens and not usable here. This tool uses\n"
            "  long-lived subscription tokens from `claude setup-token`.",
            file=sys.stderr,
        )
    print(
        "\n  Run: claude-rotate login <email> [name]\n",
        file=sys.stderr,
    )


def _to_probe_result(c: Candidate) -> ProbeResult:
    return ProbeResult(
        ok=True,
        http_code=200,
        h5_pct=c.h5_pct,
        w7_pct=c.w7_pct,
        h5_reset_secs=c.h5_reset_secs,
        w7_reset_secs=c.w7_reset_secs,
    )


def _emit_rows(
    rows: list[DashboardRow],
    *,
    chosen: str | None,
    wait_msg: str | None = None,
) -> None:
    console = Console(file=sys.stderr)
    if not console.is_terminal:
        # Non-TTY: one-liner only
        for row in rows:
            if row.account.name == chosen:
                console.print(compact_one_liner(row))
                return
        return
    render_dashboard(rows, chosen=chosen, console=console)
    render_stale_footer(rows, console=console)
    if wait_msg:
        console.print(f"  [yellow]⏳ {wait_msg}[/]")
