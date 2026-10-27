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
import os
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from claude_rotate import sessions
from claude_rotate.accounts import Store
from claude_rotate.config import SESSION_ACTIVE_WINDOW_SECONDS, Paths
from claude_rotate.dashboard import (
    NO_DATA_NOTES,
    DashboardRow,
    attach_forecast_rates,
    build_sections,
    forecast_enabled,
    relogin_row,
    render_dashboard,
    render_providers,
    render_stale_footer,
    row_from_cache,
    short_path,
    status_json,
)
from claude_rotate.groups import (
    Group,
    allowed_accounts,
    group_of,
    has_bound_groups,
    matching_group,
    session_path,
)
from claude_rotate.metadata import refresh_stale_accounts
from claude_rotate.probe import probe_many, probe_result_of
from claude_rotate.providers import ProviderQuota
from claude_rotate.providers import collect as collect_providers
from claude_rotate.report import build_report
from claude_rotate.selection import is_usable, pick_best, selection_pool
from claude_rotate.settings import load_config
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
    # The directory the snapshot was evaluated for (``session_path``-resolved) and
    # the group bound to it, if any.
    session_dir: Path
    group: str | None
    groups: Mapping[str, Group]
    # Bound groups without session isolation: ``run`` refuses to launch there.
    isolation_missing: bool
    accounts_empty: bool = False
    # Other subscriptions on this machine — display only, never part of
    # ``exit_code``: these codes describe Anthropic account health.
    providers: list[ProviderQuota] = field(default_factory=list)
    cache_age_secs: int | None = None

    @property
    def exit_code(self) -> int:
        if self.accounts_empty:
            return 3
        if self.relogin_count > 0:
            return 2
        if not self.has_usable:
            return 2
        return 0


def _collect(paths: Paths, here: Path) -> _Collected:
    """Probe every account and resolve the dashboard rows + chosen/active markers.

    ``here`` is the ``session_path``-resolved directory the snapshot is evaluated
    for: its allowed set decides the pick and the health counts, while every
    account still gets a row.

    This is the slow part (live probes + best-effort metadata refresh); the
    ``--watch`` loop calls it once per cycle while the previous frame stays on
    screen, so the redraw afterwards is flicker-free.
    """
    # Best-effort metadata refresh (same as run does)
    with contextlib.suppress(Exception):
        refresh_stale_accounts(paths)

    cfg = load_config(paths)
    group = matching_group(cfg.groups, here)
    group_name = group.name if group else None
    isolation_missing = has_bound_groups(cfg.groups) and not cfg.session_isolation
    accounts = Store(paths).load()
    if not accounts:
        return _Collected(
            rows=[],
            chosen=None,
            active=None,
            relogin_count=0,
            has_usable=False,
            session_dir=here,
            group=group_name,
            groups=cfg.groups,
            isolation_missing=isolation_missing,
            accounts_empty=True,
        )
    allowed = allowed_accounts(cfg.groups, accounts, here)

    # The third-party readers run alongside the probes rather than after them:
    # agy needs a few seconds, and that should overlap the probes, not follow.
    with ThreadPoolExecutor(max_workers=1) as providers_pool:
        provider_future = providers_pool.submit(_providers_or_empty, paths)
        candidates = probe_many(list(accounts.values()))
        providers = provider_future.result()
    cache = UsageCache(paths)

    rows: list[DashboardRow] = []
    resolved = []
    relogin_count = 0
    for c in candidates:
        if c.h5_pct is None and c.w7_pct is None:
            # Live probe failed — a 401/403 needs user action, everything else
            # falls back to the usage cache and stays in the selection pool.
            err = c.probe_error
            if err == "unauthorized":
                if c.account.name in allowed:
                    relogin_count += 1
                rows.append(relogin_row(c.account, "token invalid (401/403)"))
                continue
            filled, row = row_from_cache(c, cache, probe_error=err)
            if filled is not None:
                rows.append(row)
                resolved.append(filled)
                continue
            # No cached data either. A recognised transport failure is
            # reported as such; an unknown one most likely means a dead token.
            if err in NO_DATA_NOTES or err.split(":")[0] in NO_DATA_NOTES:
                rows.append(row)
            else:
                if c.account.name in allowed:
                    relogin_count += 1
                rows.append(relogin_row(c.account, "probe failed (token may be expired)"))
            continue
        # Keeps the last scoped values when this fetch returned none (the OAuth
        # endpoint 429s aggressively), so later backfills still have something.
        cache.save(c.account.name, probe_result_of(c))
        rows.append(
            DashboardRow(
                account=c.account,
                h5_pct=c.h5_pct,
                w7_pct=c.w7_pct,
                h5_reset_secs=c.h5_reset_secs,
                w7_reset_secs=c.w7_reset_secs,
                # The OAuth usage fetch behind scoped limits rate-limits hard;
                # backfill from the last cached value rather than hiding them.
                w7_scoped=c.w7_scoped or cache.load_scoped(c.account.name),
            )
        )
        resolved.append(c)

    # Shared with run.py so the dashboard's ``>`` marker names the account run
    # would actually launch. If nothing is eligible, ``chosen`` stays None.
    pool = selection_pool(resolved, accounts, allowed=allowed)

    chosen = None
    if pool:
        best, _ = pick_best(pool)
        chosen = best.account.name

    session = read_current_session(paths)
    active = session.account_name if session is not None else None

    loads = sessions.count_load(
        paths, now=time.time(), active_window=float(SESSION_ACTIVE_WINDOW_SECONDS)
    )
    claude_ids = sessions.claude_sessions_by_account(paths)
    rows = [
        replace(
            r,
            session_load=loads.get(r.account.name),
            claude_sessions=tuple(claude_ids.get(r.account.name, [])),
            group=group_of(cfg.groups, r.account.name),
            allowed=r.account.name in allowed,
        )
        for r in rows
    ]
    rows = attach_forecast_rates(rows, cache, time.time())

    return _Collected(
        rows=rows,
        chosen=chosen,
        active=active,
        relogin_count=relogin_count,
        has_usable=any(is_usable(c) for c in resolved if c.account.name in allowed),
        session_dir=here,
        group=group_name,
        groups=cfg.groups,
        isolation_missing=isolation_missing,
        providers=providers,
    )


def _providers_or_empty(paths: Paths) -> list[ProviderQuota]:
    """Never let a third-party reader break the Anthropic dashboard."""
    try:
        return collect_providers(paths)
    except Exception:
        return []


def _render_dashboard(collected: _Collected, console: Console) -> None:
    """Draw one box per group (one box without groups) + footers for one snapshot."""
    show_forecast = forecast_enabled()
    context = None
    if collected.groups:
        group = f"group {collected.group}" if collected.group else "no group"
        context = (
            f"{short_path(collected.session_dir)} · {group}"
            f" · next pick {collected.chosen or 'none'}"
        )
    width = render_dashboard(
        collected.rows,
        chosen=collected.chosen,
        active=collected.active,
        console=console,
        show_forecast=show_forecast,
        sections=build_sections(collected.rows, collected.groups, collected.group),
        context=context,
    )
    if collected.isolation_missing:
        line = Text(" ⚠ ", style="yellow")
        line.append(
            "Account groups need session isolation — run:"
            " claude-rotate config set session_isolation true"
        )
        console.print(line)
    render_stale_footer(collected.rows, console=console)
    # Pinned to the account table's width — the two are stacked, and the
    # forecast toggle covers both for the same reason.
    render_providers(collected.providers, console=console, width=width, show_forecast=show_forecast)


def _no_accounts_hint() -> str:
    return "  No accounts configured. Run: claude-rotate login <email> [name]"


def _json_of(collected: _Collected) -> dict[str, Any]:
    return status_json(
        collected.rows,
        chosen=collected.chosen,
        active=collected.active,
        providers=collected.providers,
        session_dir=str(collected.session_dir),
        group=collected.group,
    )


def _session_dir(session_dir: Path | None) -> Path | None:
    """The directory to evaluate for: ``--dir``, else the cwd (same rule as ``run``).

    ``None`` means ``--dir`` names no existing directory.
    """
    if session_dir is None:
        try:
            return session_path(Path.cwd())
        except OSError:  # deleted cwd; "/" is bound to nothing
            return session_path(Path("/"))
    if not os.path.isdir(os.path.expanduser(session_dir)):  # unlike Path.expanduser, never raises
        return None
    return session_path(session_dir)


def execute(
    paths: Paths,
    *,
    as_json: bool,
    report: bool = False,
    watch: float | None = None,
    session_dir: Path | None = None,
) -> int:
    console = Console(file=sys.stderr)
    here = _session_dir(session_dir)
    if here is None:
        print(f"error: no such directory: {session_dir}", file=sys.stderr)
        return 1

    # ``--watch`` only makes sense on a real terminal — when piped/captured we
    # fall through to a single render so stdout (JSON/report) stays clean.
    if watch is not None and console.is_terminal:
        return _run_watch(
            paths,
            interval=max(WATCH_MIN_SECONDS, watch),
            as_json=as_json,
            report=report,
            console=console,
            here=here,
        )

    collected = _collect(paths, here)
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
                collected.rows,
                chosen=collected.chosen,
                active=collected.active,
                fenced=fenced,
                providers=collected.providers,
            )
        )
    elif as_json:
        print(_json.dumps(_json_of(collected), indent=2))
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
    here: Path,
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
            collected = _collect(paths, here)
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
                        providers=collected.providers,
                    )
                )
            elif as_json:
                console.print_json(_json.dumps(_json_of(collected)))
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
