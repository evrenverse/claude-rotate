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

from claude_rotate.config import FORECAST_WINDOW_5H_SECONDS, FORECAST_WINDOW_7D_SECONDS

if TYPE_CHECKING:
    from claude_rotate.dashboard import DashboardRow

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

WEEKLY_RISK_PCT = 90.0
FORECAST_RISK_PCT = 100
EXPIRY_WARN_DAYS = 7
FALLBACK_MAX_WEEK_PCT = 80.0

_PLAN_LABELS = {"max_20x": "Max-20", "max_5x": "Max-5", "pro": "Pro"}


def plan_label(plan: str) -> str:
    """Human label for a plan id (``max_20x`` → ``Max-20``); empty for unknown."""
    if plan == "unknown":
        return ""
    return _PLAN_LABELS.get(plan, plan)


def compute_forecast(pct: float | None, reset_secs: int, window_secs: int) -> int | None:
    """Linear projection of where ``pct`` lands at window reset.

    Stateless — relies only on the current percentage and seconds-until-reset.
    Truncates exactly like the Bash statusline (``int(pct)`` + floor division)
    so the dashboard and statusline show the same figure for the same inputs.
    Returns ``None`` when there is no usable elapsed time (no active window or
    a brand-new window) or when usage is already at/over the limit (``pct >=
    100`` — the projection would only be noise), 0 for zero usage, and caps the
    result at 999.
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
    return min(999, int(pct) * window_secs // elapsed)


def compute_limit_eta(pct: float | None, reset_secs: int, window_secs: int) -> int | None:
    """Seconds-from-now until usage is projected to reach 100%.

    Linear projection at the current rate, mirroring ``compute_forecast``'s
    ``int(pct)`` truncation so the forecast % and this ETA stay consistent.
    Returns ``None`` when there is no usable elapsed time (no active / brand-new
    window), when usage is zero or already at/over the limit, or when the window
    resets before 100% is reached (equivalently: ``compute_forecast`` would be
    ``<= 100``, so there is no limit hit to forecast inside this window).
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
    if eta >= reset_secs:
        return None  # window resets before the wall is reached
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
    """Per-account risk messages (no prefix/bullet — callers add their own)."""
    warns: list[str] = []
    for row in rows:
        name = row.account.name
        tag = " (active)" if name == active else ""
        if row.status != "ok":
            detail = f" — {row.note}" if row.note else ""
            warns.append(f"{name}{tag}: {row.status}{detail}.")
            continue
        w7 = row.w7_pct
        w7_forecast = compute_forecast(w7, row.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS)
        over_forecast = w7_forecast is not None and w7_forecast > FORECAST_RISK_PCT
        if w7 is not None and (w7 >= WEEKLY_RISK_PCT or over_forecast):
            parts = [f"week {w7:g}%"]
            if w7_forecast is not None and over_forecast:
                parts.append(f"forecast {w7_forecast}%")
            warns.append(f"{name}{tag}: {', '.join(parts)} → weekly limit at risk.")
        h5_forecast = compute_forecast(row.h5_pct, row.h5_reset_secs, FORECAST_WINDOW_5H_SECONDS)
        if h5_forecast is not None and h5_forecast > FORECAST_RISK_PCT:
            warns.append(f"{name}{tag}: 5h forecast {h5_forecast}% → 5h limit at risk.")
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
