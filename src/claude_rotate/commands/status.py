"""`claude-rotate status` — dashboard only, health-reflecting exit codes.

Exit codes (from the spec):
  0 — all healthy, at least one usable
  2 — one or more accounts need re-login OR no usable account right now
  3 — no accounts configured
  4 — dashboard / network error
  5 — lock timeout (handled by caller)
"""

from __future__ import annotations

import contextlib
import json as _json
import sys
import time as _time
from dataclasses import replace

from rich.console import Console

from claude_rotate import sessions
from claude_rotate.accounts import Store
from claude_rotate.config import SESSION_ACTIVE_WINDOW_SECONDS, Paths
from claude_rotate.dashboard import (
    DashboardRow,
    forecast_enabled,
    render_dashboard,
    render_stale_footer,
    status_json,
)
from claude_rotate.metadata import refresh_stale_accounts
from claude_rotate.probe import probe_many
from claude_rotate.report import build_report
from claude_rotate.selection import is_usable, pick_best
from claude_rotate.sync import read_current_session
from claude_rotate.usage_cache import UsageCache


def execute(paths: Paths, *, as_json: bool, report: bool = False) -> int:
    # Best-effort metadata refresh (same as run does)
    with contextlib.suppress(Exception):
        refresh_stale_accounts(paths)

    accounts = Store(paths).load()
    if not accounts:
        print(
            "  No accounts configured. Run: claude-rotate login <email> [name]",
            file=sys.stderr,
        )
        return 3

    candidates = probe_many(list(accounts.values()))
    cache = UsageCache(paths)

    rows: list[DashboardRow] = []
    resolved = []
    relogin_count = 0
    for c in candidates:
        if c.h5_pct is None and c.w7_pct is None:
            # Live probe failed — classify by the error type
            err = c.probe_error
            if err == "unauthorized":
                relogin_count += 1
                rows.append(
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
            if err == "rate_limited":
                # A 429 without rate-limit headers is a probe failure, not
                # trustworthy quota data. Prefer cached numbers; keep the
                # candidate in ``resolved`` so selection can still consider it.
                cached = cache.load(c.account.name)
                if cached is not None:
                    c = replace(
                        c,
                        h5_pct=cached.h5_pct,
                        w7_pct=cached.w7_pct,
                        h5_reset_secs=cached.h5_reset_secs,
                        w7_reset_secs=cached.w7_reset_secs,
                    )
                    rows.append(
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
                    rows.append(
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
            if err == "upstream_error":
                cached = cache.load(c.account.name)
                if cached is not None:
                    c = replace(
                        c,
                        h5_pct=cached.h5_pct,
                        w7_pct=cached.w7_pct,
                        h5_reset_secs=cached.h5_reset_secs,
                        w7_reset_secs=cached.w7_reset_secs,
                    )
                    rows.append(
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
                    continue
                rows.append(
                    DashboardRow(
                        account=c.account,
                        h5_pct=None,
                        w7_pct=None,
                        h5_reset_secs=0,
                        w7_reset_secs=0,
                        status="no_data",
                        note="API 5xx — retry later",
                    )
                )
                continue
            if err == "timeout" or err.startswith("network_error"):
                cached = cache.load(c.account.name)
                if cached is not None:
                    c = replace(
                        c,
                        h5_pct=cached.h5_pct,
                        w7_pct=cached.w7_pct,
                        h5_reset_secs=cached.h5_reset_secs,
                        w7_reset_secs=cached.w7_reset_secs,
                    )
                    rows.append(
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
                    continue
                rows.append(
                    DashboardRow(
                        account=c.account,
                        h5_pct=None,
                        w7_pct=None,
                        h5_reset_secs=0,
                        w7_reset_secs=0,
                        status="no_data",
                        note="network error",
                    )
                )
                continue
            # Unknown error or no error string — try cache fallback
            cached = cache.load(c.account.name)
            if cached is not None:
                c = replace(
                    c,
                    h5_pct=cached.h5_pct,
                    w7_pct=cached.w7_pct,
                    h5_reset_secs=cached.h5_reset_secs,
                    w7_reset_secs=cached.w7_reset_secs,
                )
                rows.append(
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
                continue
            relogin_count += 1
            rows.append(
                DashboardRow(
                    account=c.account,
                    h5_pct=None,
                    w7_pct=None,
                    h5_reset_secs=0,
                    w7_reset_secs=0,
                    status="relogin",
                    note="probe failed (token may be expired)",
                )
            )
            continue
        rows.append(
            DashboardRow(
                account=c.account,
                h5_pct=c.h5_pct,
                w7_pct=c.w7_pct,
                h5_reset_secs=c.h5_reset_secs,
                w7_reset_secs=c.w7_reset_secs,
            )
        )
        resolved.append(c)

    # Selection must honour disabling + pinning — same logic as run.py so the
    # dashboard agrees on which account gets the ``>``/``★`` marker. Disabled
    # accounts are excluded entirely (and never fall back into the pool); if
    # nothing usable remains, ``chosen`` stays None.
    pinned_names = {a.name for a in accounts.values() if a.pinned}
    disabled_names = {a.name for a in accounts.values() if a.disabled}
    enabled = [c for c in resolved if c.account.name not in disabled_names]
    selection_pool = (
        [c for c in enabled if c.account.name in pinned_names] if pinned_names else enabled
    )
    if not selection_pool:
        selection_pool = enabled  # pinned failed probe → fall back to enabled only

    chosen = None
    if selection_pool:
        best, _ = pick_best(selection_pool)
        chosen = best.account.name

    session = read_current_session(paths)
    active = session.account_name if session is not None else None

    loads = sessions.count_load(
        paths, now=_time.time(), active_window=float(SESSION_ACTIVE_WINDOW_SECONDS)
    )
    rows = [replace(r, session_load=loads.get(r.account.name)) for r in rows]

    if report:
        # Fenced (Markdown code block) only when piped/captured — e.g. by the
        # bundled skill, which relays the output into a chat UI. A real
        # terminal gets the clean table without the ``` fences.
        fenced = not sys.stdout.isatty()
        print(build_report(rows, chosen=chosen, active=active, fenced=fenced))
    elif as_json:
        print(_json.dumps(status_json(rows, chosen=chosen, active=active), indent=2))
    else:
        console = Console(file=sys.stderr)
        render_dashboard(
            rows,
            chosen=chosen,
            active=active,
            console=console,
            show_forecast=forecast_enabled(),
        )
        render_stale_footer(rows, console=console)

    if relogin_count > 0:
        return 2
    if not resolved or not any(is_usable(c) for c in resolved):
        return 2
    return 0
