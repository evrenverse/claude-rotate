"""`claude-rotate run …` — the default command path."""

from __future__ import annotations

import contextlib
import os
import sys
import time
import uuid as uuid_lib
from dataclasses import replace
from datetime import UTC, datetime

from rich.console import Console

from claude_rotate import sessions
from claude_rotate.accounts import Store
from claude_rotate.config import (
    SESSION_ACTIVE_WINDOW_SECONDS,
    SESSION_IDLE_WEIGHT,
    Paths,
)
from claude_rotate.dashboard import (
    DashboardRow,
    attach_forecast_rates,
    compact_one_liner,
    forecast_enabled,
    render_dashboard,
    render_stale_footer,
)
from claude_rotate.exec import exec_claude
from claude_rotate.metadata import refresh_stale_accounts
from claude_rotate.probe import ProbeResult, probe_many
from claude_rotate.refresh import ensure_fresh
from claude_rotate.selection import Candidate, is_usable, pick_best
from claude_rotate.settings import load_config
from claude_rotate.state_log import StateLog
from claude_rotate.sync import reconcile_all, refresh_stale_tokens
from claude_rotate.usage_cache import UsageCache


def _reserve_record(paths: Paths, account_name: str) -> str:
    """Write a session-registry record for this run and return its uuid.

    Reads our own pid + start_time — both survive execvpe, so the record points
    at the claude process we are about to become. Best-effort.
    """
    run_uuid = uuid_lib.uuid4().hex
    pid = os.getpid()
    now = time.time()
    sessions.write_record(
        paths,
        sessions.SessionRecord(
            uuid=run_uuid,
            account=account_name,
            pid=pid,
            start_time=sessions.process_start_time(pid) or 0.0,
            started_at=now,
            last_active=now,
        ),
    )
    return run_uuid


def _rewrite_pinned_wait(
    wait_msg: str | None,
    pinned_names: set[str],
    best: Candidate,
    enabled_resolved: list[Candidate],
) -> str | None:
    if not (wait_msg and pinned_names):
        return wait_msg
    has_alternative = any(
        c.account.name not in pinned_names and is_usable(c) for c in enabled_resolved
    )
    in_idx = wait_msg.find(" in ")
    remainder = wait_msg[in_idx:] if in_idx != -1 else ""
    msg = f"pinned account {best.account.label!r} exhausted; available{remainder}"
    if has_alternative:
        msg += " — run `claude-rotate unpin` to rotate instead"
    return msg


def execute(paths: Paths, claude_args: list[str]) -> int:
    # Best-effort; never blocks or raises
    with contextlib.suppress(Exception):
        refresh_stale_accounts(paths)

    # Pre-run reconcile: pull any drift the cron hasn't picked up yet.
    with contextlib.suppress(Exception):
        from claude_rotate.sync import reconcile_isolated

        if load_config(paths).session_isolation:
            reconcile_isolated(paths, now=datetime.now(UTC))
        else:
            reconcile_all(paths, now=datetime.now(UTC))

    store = Store(paths)
    accounts = store.load()
    if not accounts:
        _print_no_accounts_message()
        return 3

    # Every account manually disabled → refuse before probing. Launching a
    # disabled account would defeat the whole point of `disable`.
    if all(a.disabled for a in accounts.values()):
        _print_all_disabled_message()
        return 3

    # Proactively refresh any account whose access token is stale BEFORE
    # we probe. Without this the probe hits Anthropic with a dead token,
    # the dashboard flags the account as 'relogin', and — even though a
    # later ensure_fresh in the chosen-account path would have recovered
    # — the child claude boots with whatever we could refresh (or not)
    # and may show the dreaded login prompt. Refreshing up-front keeps
    # every account usable and the dashboard honest.
    with contextlib.suppress(Exception):
        isolated = load_config(paths).session_isolation
        refreshed = refresh_stale_tokens(paths, now=datetime.now(UTC), isolated=isolated)
        if refreshed:
            # accounts.json was rewritten — reload so probe sees fresh tokens
            accounts = store.load()

    # Probe every account so the dashboard is always complete — pinning
    # only constrains the selection pool, not what the user sees.
    pool = list(accounts.values())
    pinned_names = {a.name for a in pool if a.pinned}
    disabled_names = {a.name for a in pool if a.disabled}

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
            # A 429 without rate-limit headers is a probe failure, not
            # trustworthy quota data. Fall back to cached usage values and
            # keep the candidate in the selection pool.
            if err == "rate_limited":
                cached = cache.load(c.account.name)
                if cached is not None:
                    c = replace(
                        c,
                        h5_pct=cached.h5_pct,
                        w7_pct=cached.w7_pct,
                        h5_reset_secs=cached.h5_reset_secs,
                        w7_reset_secs=cached.w7_reset_secs,
                        w7_opus_pct=cached.w7_opus_pct,
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
                    w7_opus_pct=cached.w7_opus_pct,
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

    # Stamp the recent-burn rates from the usage history so the rendered forecast
    # weights the recent tail, not just the window average. (Selection/routing
    # still uses the average pace — see selection.py.)
    dashboard_rows = attach_forecast_rates(dashboard_rows, cache, time.time())

    # Disabled accounts are never selection candidates (they remain in
    # dashboard_rows so the user still sees them, greyed out).
    enabled_resolved = [c for c in resolved if c.account.name not in disabled_names]

    cfg = load_config(paths)

    if not enabled_resolved:
        # No probe data for any *enabled* account → exec the first enabled
        # account with its cached token. Prefer a pinned-enabled one. An
        # enabled account is guaranteed to exist (all-disabled returned above).
        first = next(
            (a for a in accounts.values() if a.pinned and not a.disabled),
            next(a for a in accounts.values() if not a.disabled),
        )
        StateLog(paths).event("exec", chosen=first.name, reason="no_probe_data")
        _emit_rows(dashboard_rows, chosen=first.name)
        fresh = ensure_fresh(first, paths)
        if cfg.session_tracking:
            return exec_claude(
                fresh, paths, claude_args, session_uuid=_reserve_record(paths, first.name)
            )
        return exec_claude(fresh, paths, claude_args)

    # Pinning: restrict the (already disabled-filtered) pool to the pinned
    # account(s) only. Non-pinned accounts stay in dashboard_rows so the user
    # still sees them.
    selection_pool = (
        [c for c in enabled_resolved if c.account.name in pinned_names]
        if pinned_names
        else enabled_resolved
    )
    if not selection_pool:
        # Pinned account(s) all failed live probe; fall back to the enabled
        # set — never to a disabled account.
        selection_pool = enabled_resolved

    if not cfg.session_tracking:
        # Tracking disabled → exact pre-feature behaviour.
        best, wait_msg = pick_best(selection_pool)
        wait_msg = _rewrite_pinned_wait(wait_msg, pinned_names, best, enabled_resolved)
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

    with sessions.file_lock(paths.sessions_lock):
        loads = sessions.count_load(
            paths, now=time.time(), active_window=float(SESSION_ACTIVE_WINDOW_SECONDS)
        )
        loaded_pool = [
            replace(
                c,
                session_load=loads[c.account.name].weighted(idle_weight=SESSION_IDLE_WEIGHT)
                if c.account.name in loads
                else 0.0,
            )
            for c in selection_pool
        ]
        best, wait_msg = pick_best(loaded_pool)
        run_uuid = _reserve_record(paths, best.account.name)
        # If exec_claude fails below, this record is orphaned briefly but our
        # own pid dies with the failed run, so the next count_load reaps it.

    wait_msg = _rewrite_pinned_wait(wait_msg, pinned_names, best, enabled_resolved)
    _emit_rows(dashboard_rows, chosen=best.account.name, wait_msg=wait_msg)
    StateLog(paths).event(
        "exec",
        chosen=best.account.name,
        h5_pct=best.h5_pct,
        w7_pct=best.w7_pct,
        wait=wait_msg,
    )
    fresh = ensure_fresh(best.account, paths)
    return exec_claude(fresh, paths, claude_args, session_uuid=run_uuid)


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


def _print_all_disabled_message() -> None:
    print("", file=sys.stderr)
    print("  All accounts are manually disabled — nothing to rotate.", file=sys.stderr)
    print(
        "\n  Re-enable one with: claude-rotate enable <name>\n",
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
        w7_opus_pct=c.w7_opus_pct,
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
    render_dashboard(rows, chosen=chosen, console=console, show_forecast=forecast_enabled())
    render_stale_footer(rows, console=console)
    if wait_msg:
        console.print(f"  [yellow]⏳ {wait_msg}[/]")
