"""Shared quota semantics for the status dashboard and the ``--report`` cards.

Pure computations and message builders used by both renderers
(``dashboard.render_dashboard`` and ``report.build_report``) so forecasts,
risk thresholds and wording live in exactly one place. This module must not
import the renderers at runtime — ``dashboard`` re-exports the forecast
helpers for backwards compatibility, so an import in the other direction
would be circular.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from claude_rotate.config import FORECAST_WINDOW_7D_SECONDS

if TYPE_CHECKING:
    from claude_rotate.dashboard import DashboardRow

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

EXPIRY_WARN_DAYS = 7
FALLBACK_MAX_WEEK_PCT = 80.0

_PLAN_LABELS = {"max_20x": "Max-20", "max_5x": "Max-5", "pro": "Pro"}


def plan_label(plan: str) -> str:
    """Human label for a plan id (``max_20x`` → ``Max-20``); empty for unknown."""
    if plan == "unknown":
        return ""
    return _PLAN_LABELS.get(plan, plan)


def seconds_until(moment: datetime | None, now: datetime) -> int | None:
    """Whole seconds from ``now`` until ``moment`` (clamped to >= 0); ``None`` if no moment."""
    if moment is None:
        return None
    return max(0, int((moment - now).total_seconds()))


def expiry_horizon(expires_at: datetime | None, reset_secs: int, now: datetime) -> int | None:
    """Forecast horizon capped at subscription expiry, or ``None`` when the window resets first.

    Returns the seconds-until-expiry only when the subscription dies strictly before the
    window reset (``0 < expiry < reset_secs``); otherwise ``None`` (no cap). Pairs with
    ``compute_forecast``/``compute_limit_eta``'s ``horizon_secs`` argument, and doubles as
    the renderers' "is this cell capped?" signal (non-``None`` == capped).
    """
    expiry = seconds_until(expires_at, now)
    if expiry is not None and 0 < expiry < reset_secs:
        return expiry  # non-None doubles as the renderers' "is capped?" signal
    return None


def compute_forecast(
    pct: float | None,
    reset_secs: int,
    window_secs: int,
    horizon_secs: int | None = None,
) -> int | None:
    """Linear projection of where ``pct`` lands at the projection horizon.

    The horizon defaults to the window reset (``reset_secs``); pass ``horizon_secs`` to cap
    it earlier (e.g. at subscription expiry). ``elapsed = window_secs - reset_secs`` and is
    unaffected by the horizon — only the projection horizon shortens. With ``horizon_secs is
    None`` the result is bit-identical to projecting to the reset. Same truncation as the Bash
    statusline; returns ``None`` for no usable elapsed time or ``pct >= 100``, 0 for zero
    usage, caps at 999.
    """
    if pct is None or reset_secs <= 0:
        return None
    elapsed = window_secs - reset_secs
    if elapsed <= 0:
        return None
    if pct <= 0:
        return 0
    if pct >= 100:
        return None
    horizon = reset_secs if horizon_secs is None else min(reset_secs, horizon_secs)
    return min(999, int(pct) * (elapsed + horizon) // elapsed)


def compute_limit_eta(
    pct: float | None,
    reset_secs: int,
    window_secs: int,
    horizon_secs: int | None = None,
) -> int | None:
    """Seconds-from-now until usage is projected to reach 100%, within the horizon.

    The horizon defaults to the window reset; pass ``horizon_secs`` to cap it earlier.
    Returns ``None`` when there is no usable elapsed time, usage is zero or already >= 100,
    or the projected wall lands at/after the horizon (window resets — or the subscription
    expires — before 100% is reached).
    """
    if pct is None or reset_secs <= 0:
        return None
    elapsed = window_secs - reset_secs
    if elapsed <= 0:
        return None
    p = int(pct)
    if p <= 0 or p >= 100:
        return None
    eta = (100 - p) * elapsed // p
    horizon = reset_secs if horizon_secs is None else min(reset_secs, horizon_secs)
    if eta >= horizon:
        return None
    return eta


def pct_str(value: float | None) -> str:
    return f"{value:g}%" if value is not None else "N/A"


def rel_duration(reset_secs: int) -> str:
    """Compact relative time-to-reset in parentheses: (40m), (1h 3m), (4d 20h)."""
    secs = max(reset_secs, 0)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"({days}d {hours}h)"
    if hours:
        return f"({hours}h {minutes}m)"
    return f"({minutes}m)"


def clock_at(now: datetime, secs: int, *, show_weekday: bool) -> str:
    """Absolute local clock ``secs`` from ``now``; optional shared weekday slot.

    With ``show_weekday`` the weekday is rendered only when the moment lands on
    another calendar day; same-day clocks get a blank slot of equal width so
    columns stay aligned.
    """
    moment = now + timedelta(seconds=max(secs, 0))
    clock = moment.strftime("%H:%M")
    if show_weekday:
        weekday = WEEKDAYS[moment.weekday()] if moment.date() != now.date() else "   "
        clock = f"{weekday} {clock}"
    return clock


def days_left(expires_at: datetime | None, now_utc: datetime) -> str:
    if expires_at is None:
        return "-"
    return f"{(expires_at - now_utc).days}d"


def status_line(active: str | None, chosen: str | None) -> str:
    if active is None:
        if chosen is None:
            return "No active session and no rotation pick available."
        return f"No active session recorded; next launch would pick '{chosen}' (>)."
    if active == chosen:
        return f"Session runs on '{active}' (@); it is also the next pick (>), so no rotation."
    if chosen is None:
        return f"Session runs on '{active}' (@)."
    return f"Session runs on '{active}' (@); next launch rotates to '{chosen}' (>)."


def warning_messages(
    rows: Sequence[DashboardRow], *, active: str | None, now_utc: datetime
) -> list[str]:
    """Per-account action-needed messages (no prefix/bullet — callers add their own).

    Only signals that call for action: an account that needs re-login (any
    ``status`` other than ``ok``) or a subscription expiring within
    ``EXPIRY_WARN_DAYS``. Quota usage/forecast risk is intentionally excluded —
    the per-account bars already carry usage, and the bare projection is noise.
    """
    warns: list[str] = []
    for row in rows:
        name = row.account.name
        tag = " (active)" if name == active else ""
        if row.status != "ok":
            detail = f" — {row.note}" if row.note else ""
            warns.append(f"{name}{tag}: {row.status}{detail}.")
            continue
        expires_at = row.account.effective_expires_at
        if expires_at is not None:
            days = (expires_at - now_utc).days
            if days < EXPIRY_WARN_DAYS:
                warns.append(f"{name}{tag}: subscription expires in {days}d.")
    return warns


def fallback_account(rows: Sequence[DashboardRow], *, active: str | None) -> DashboardRow | None:
    """The non-active account with the most weekly headroom, if any is roomy."""
    others = [r for r in rows if r.account.name != active and r.w7_pct is not None]
    if not others:
        return None

    def headroom(row: DashboardRow) -> tuple[float, float]:
        used = row.w7_pct if row.w7_pct is not None else 1e9
        forecast = compute_forecast(row.w7_pct, row.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS)
        return (used, float(forecast) if forecast is not None else 1e9)

    spare = min(others, key=headroom)
    if spare.w7_pct is not None and spare.w7_pct < FALLBACK_MAX_WEEK_PCT:
        return spare
    return None


def fallback_note(rows: Sequence[DashboardRow], *, active: str | None) -> str | None:
    """Human-readable fallback recommendation, or ``None`` when nothing is roomy."""
    fallback = fallback_account(rows, active=active)
    if fallback is None:
        return None
    forecast = compute_forecast(fallback.w7_pct, fallback.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS)
    extra = f", forecast {forecast}%" if forecast is not None else ""
    week = pct_str(fallback.w7_pct)
    return f"Fallback: {fallback.account.name} (week {week}{extra})."
