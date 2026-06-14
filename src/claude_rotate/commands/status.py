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
import time
from dataclasses import dataclass, replace
from datetime import datetime

from rich.console import Console
from rich.text import Text

from claude_rotate.accounts import Store
from claude_rotate.config import Paths
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

# Default cadence for ``--watch`` when no interval is given, and the floor we
# clamp any user-supplied interval to so a live view never hammers the probe API.
WATCH_DEFAULT_SECONDS = 5.0
WATCH_MIN_SECONDS = 1.0


@dataclass(frozen=True)
class _Collected:
    """One snapshot: dashboard rows plus the derived selection + health summary."""

    rows: list[DashboardRow]
    chosen: str | None
    active: str | None
    relogin_count: int
    has_usable: bool
    accounts_empty: bool = False

    @property
    def exit_code(self) -> int:
        if self.accounts_empty:
            return 3
        if self.relogin_count > 0:
            return 2
        if not self.has_usable:
            return 2
        return 0


def _collect(paths: Paths) -> _Collected:
    """Probe every account and resolve the dashboard rows + chosen/active markers.

    This is the slow part (live probes + best-effort metadata refresh); the
    ``--watch`` loop calls it once per cycle while the previous frame stays on
    screen, so the redraw afterwards is flicker-free.
    """
    # Best-effort metadata refresh (same as run does)
    with contextlib.suppress(Exception):
        refresh_stale_accounts(paths)

    accounts = Store(paths).load()
    if not accounts:
        return _Collected(
            rows=[],
            chosen=None,
            active=None,
            relogin_count=0,
            has_usable=False,
            accounts_empty=True,
        )

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

    return _Collected(
        rows=rows,
        chosen=chosen,
        active=active,
        relogin_count=relogin_count,
        has_usable=bool(resolved) and any(is_usable(c) for c in resolved),
    )


def _render_dashboard(collected: _Collected, console: Console) -> None:
    """Draw the coloured table + stale-metadata footer for one snapshot."""
    render_dashboard(
        collected.rows,
        chosen=collected.chosen,
        active=collected.active,
        console=console,
        show_forecast=forecast_enabled(),
    )
    render_stale_footer(collected.rows, console=console)


def _no_accounts_hint() -> str:
    return "  No accounts configured. Run: claude-rotate login <email> [name]"


def execute(
    paths: Paths,
    *,
    as_json: bool,
    report: bool = False,
    watch: float | None = None,
) -> int:
    console = Console(file=sys.stderr)

    # ``--watch`` only makes sense on a real terminal — when piped/captured we
    # fall through to a single render so stdout (JSON/report) stays clean.
    if watch is not None and console.is_terminal:
        return _run_watch(
            paths,
            interval=max(WATCH_MIN_SECONDS, watch),
            as_json=as_json,
            report=report,
            console=console,
        )

    collected = _collect(paths)
    if collected.accounts_empty:
        print(_no_accounts_hint(), file=sys.stderr)
        return 3

    if report:
        # Fenced (Markdown code block) only when piped/captured — e.g. by the
        # bundled skill, which relays the output into a chat UI. A real
        # terminal gets the clean table without the ``` fences.
        fenced = not sys.stdout.isatty()
        print(
            build_report(
                collected.rows, chosen=collected.chosen, active=collected.active, fenced=fenced
            )
        )
    elif as_json:
        print(
            _json.dumps(
                status_json(collected.rows, chosen=collected.chosen, active=collected.active),
                indent=2,
            )
        )
    else:
        _render_dashboard(collected, console)

    return collected.exit_code


def _render_watch_footer(console: Console, interval: float) -> None:
    """Live-view chrome: local refresh time, cadence, and how to quit."""
    now_local = datetime.now().astimezone()
    secs = int(interval) if float(interval).is_integer() else interval
    line = Text("\n")
    line.append(f"  ⟳ {now_local:%H:%M:%S}", style="cyan")
    line.append(f" · refreshing every {secs}s · Ctrl-C to quit", style="dim")
    console.print(line)


def _run_watch(
    paths: Paths,
    *,
    interval: float,
    as_json: bool,
    report: bool,
    console: Console,
) -> int:
    """Re-probe and redraw on the alternate screen every ``interval`` seconds.

    Each cycle collects a fresh snapshot (the slow probe) while the previous
    frame is still on screen, then clears and redraws — so the view updates
    without a visible blank gap. Ctrl-C exits cleanly, restoring the terminal.
    """
    console.set_alt_screen(True)
    console.show_cursor(False)
    last_code = 0
    try:
        console.print("  Probing accounts…", style="dim")
        while True:
            collected = _collect(paths)
            console.clear()
            if collected.accounts_empty:
                console.print(_no_accounts_hint())
            elif report:
                console.print(
                    build_report(
                        collected.rows,
                        chosen=collected.chosen,
                        active=collected.active,
                        fenced=False,
                    )
                )
            elif as_json:
                console.print_json(
                    _json.dumps(
                        status_json(
                            collected.rows, chosen=collected.chosen, active=collected.active
                        )
                    )
                )
            else:
                _render_dashboard(collected, console)
            if not collected.accounts_empty:
                _render_watch_footer(console, interval)
            last_code = collected.exit_code
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        console.show_cursor(True)
        console.set_alt_screen(False)
    return last_code
