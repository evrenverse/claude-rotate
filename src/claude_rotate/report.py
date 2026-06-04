"""`claude-rotate status --report` — a compact, single-table account overview.

Where ``render_dashboard`` (dashboard.py) is the rich, colour-bar quota view,
this module renders a plain box-drawing table designed to be relayed verbatim
by the bundled Claude Code skill (see ``skill_assets/account``). It answers a
narrower question — *which account is this session on, what are the limits, and
what should I watch out for* — and is therefore a separate, self-contained
renderer rather than another column on the dashboard.

Two markers identify accounts:

* ``@`` — the account this session is currently running on
  (``current-session.json``).
* ``>`` — the account the rotator would pick on the next launch (``chosen``).
* ``@>`` — both, i.e. the session is already on the next pick (no rotation).

All values are place-value aligned: every numeric sub-field is right-padded to
its column's widest entry, so ``23m`` sits exactly under ``53m`` and ``8h``
under ``12h``. Reset cells follow the claude-statusline convention: absolute
clock, plus an English weekday when the reset lands on another day, plus the
relative duration in parentheses.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from claude_rotate.config import (
    FORECAST_WINDOW_5H_SECONDS,
    FORECAST_WINDOW_7D_SECONDS,
)
from claude_rotate.dashboard import DashboardRow, compute_forecast

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_WEEKLY_RISK_PCT = 90.0
_FORECAST_RISK_PCT = 100
_EXPIRY_WARN_DAYS = 7
_FALLBACK_MAX_WEEK_PCT = 80.0


def _box_table(headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: Sequence[str]) -> str:
    """Render a bordered box-drawing table; equal-width rows keep columns aligned."""
    cols = range(len(headers))
    widths = [len(headers[i]) for i in cols]
    for row in rows:
        for i in cols:
            widths[i] = max(widths[i], len(row[i]))

    def cell(value: str, i: int) -> str:
        return value.rjust(widths[i]) if aligns[i] == "r" else value.ljust(widths[i])

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (widths[i] + 2) for i in cols) + right

    def line(values: Sequence[str]) -> str:
        return "│ " + " │ ".join(cell(values[i], i) for i in cols) + " │"

    parts = [rule("┌", "┬", "┐"), line(headers), rule("├", "┼", "┤")]
    parts.extend(line(row) for row in rows)
    parts.append(rule("└", "┴", "┘"))
    return "\n".join(parts)


def format_reset_column(secs_list: Sequence[int | None], *, now: datetime) -> list[str]:
    """Format a whole reset column so every place value lines up vertically.

    ``None`` entries (no data for that row) render as ``-``. The column shares
    one structure: both fields are always shown (even ``0h``), the weekday is
    shown for every row as soon as *any* reset lands on another day, and each
    numeric sub-field is right-padded to the column's widest value.
    """
    cleaned = [max(s, 0) if s is not None else None for s in secs_list]
    valid = [s for s in cleaned if s is not None]
    if not valid:
        return ["-" for _ in secs_list]

    day_scale = max(valid) >= 86400
    resets = [now + timedelta(seconds=s) if s is not None else None for s in cleaned]
    show_weekday = any(r is not None and r.date() != now.date() for r in resets)

    fields: list[tuple[int, int] | None] = []
    for s in cleaned:
        if s is None:
            fields.append(None)
        elif day_scale:
            fields.append((s // 86400, (s % 86400) // 3600))
        else:
            fields.append((s // 3600, (s % 3600) // 60))
    w0 = max((len(str(f[0])) for f in fields if f is not None), default=1)
    w1 = max((len(str(f[1])) for f in fields if f is not None), default=1)
    unit0, unit1 = ("d", "h") if day_scale else ("h", "m")

    out: list[str] = []
    for reset, field in zip(resets, fields, strict=True):
        if reset is None or field is None:
            out.append("-")
            continue
        clock = reset.strftime("%H:%M")
        if show_weekday:
            weekday = WEEKDAYS[reset.weekday()] if reset.date() != now.date() else "   "
            clock = f"{weekday} {clock}"
        rel = f"{field[0]:>{w0}d}{unit0} {field[1]:>{w1}d}{unit1}"
        out.append(f"{clock} ({rel})")
    return out


def _pct(value: float | None) -> str:
    return f"{value:g}%" if value is not None else "N/A"


def _days_left(expires_at: datetime | None, now_utc: datetime) -> str:
    if expires_at is None:
        return "-"
    return f"{(expires_at - now_utc).days}d"


def _status_line(active: str | None, chosen: str | None) -> str:
    if active is None:
        if chosen is None:
            return "No active session and no rotation pick available."
        return f"No active session recorded; next launch would pick '{chosen}' (>)."
    if active == chosen:
        return f"Session runs on '{active}' (@); it is also the next pick (>), so no rotation."
    if chosen is None:
        return f"Session runs on '{active}' (@)."
    return f"Session runs on '{active}' (@); next launch rotates to '{chosen}' (>)."


def _warnings(rows: Sequence[DashboardRow], *, active: str | None, now_utc: datetime) -> list[str]:
    warns: list[str] = []
    for row in rows:
        name = row.account.name
        tag = " (active)" if name == active else ""
        if row.status != "ok":
            detail = f" — {row.note}" if row.note else ""
            warns.append(f"- {name}{tag}: {row.status}{detail}.")
            continue
        w7 = row.w7_pct
        w7_forecast = compute_forecast(w7, row.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS)
        over_forecast = w7_forecast is not None and w7_forecast > _FORECAST_RISK_PCT
        if w7 is not None and (w7 >= _WEEKLY_RISK_PCT or over_forecast):
            parts = [f"week {w7:g}%"]
            if w7_forecast is not None and over_forecast:
                parts.append(f"forecast {w7_forecast}%")
            warns.append(f"- {name}{tag}: {', '.join(parts)} → weekly limit at risk.")
        h5_forecast = compute_forecast(row.h5_pct, row.h5_reset_secs, FORECAST_WINDOW_5H_SECONDS)
        if h5_forecast is not None and h5_forecast > _FORECAST_RISK_PCT:
            warns.append(f"- {name}{tag}: 5h forecast {h5_forecast}% → 5h limit at risk.")
        expires_at = row.account.effective_expires_at
        if expires_at is not None:
            days = (expires_at - now_utc).days
            if days < _EXPIRY_WARN_DAYS:
                warns.append(f"- {name}{tag}: subscription expires in {days}d.")

    # Nothing wrong → no fallback advice; keep the all-clear line meaningful.
    if not warns:
        return ["✅ All accounts healthy."]

    fallback = _fallback_account(rows, active=active)
    if fallback is not None:
        forecast = compute_forecast(
            fallback.w7_pct, fallback.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS
        )
        extra = f", forecast {forecast}%" if forecast is not None else ""
        week = _pct(fallback.w7_pct)
        warns.append(f"- Fallback: {fallback.account.name} (week {week}{extra}).")

    return ["⚠️ Warnings:", *warns]


def _fallback_account(rows: Sequence[DashboardRow], *, active: str | None) -> DashboardRow | None:
    """The non-active account with the most weekly headroom, if any is roomy."""
    others = [r for r in rows if r.account.name != active and r.w7_pct is not None]
    if not others:
        return None

    def headroom(row: DashboardRow) -> tuple[float, float]:
        used = row.w7_pct if row.w7_pct is not None else 1e9
        forecast = compute_forecast(row.w7_pct, row.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS)
        return (used, float(forecast) if forecast is not None else 1e9)

    spare = min(others, key=headroom)
    if spare.w7_pct is not None and spare.w7_pct < _FALLBACK_MAX_WEEK_PCT:
        return spare
    return None


def build_report(
    rows: Sequence[DashboardRow],
    *,
    chosen: str | None,
    active: str | None,
    now: datetime | None = None,
    fenced: bool = True,
) -> str:
    """Build the full account report as a ready-to-display string.

    ``active`` is the account this session runs on (from current-session.json);
    ``chosen`` is the rotator's next pick. ``fenced`` wraps the table in a
    Markdown code fence so it renders monospaced when relayed into a chat UI;
    callers pass ``fenced=False`` for a raw terminal.
    """
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()
    now_utc = now.astimezone(UTC)

    def sort_key(row: DashboardRow) -> tuple[int, int]:
        name = row.account.name
        return (0 if name == active else 1, 0 if name == chosen else 1)

    ordered = sorted(rows, key=sort_key)

    h5_resets = format_reset_column(
        [r.h5_reset_secs if r.h5_pct is not None else None for r in ordered], now=now
    )
    w7_resets = format_reset_column(
        [r.w7_reset_secs if r.w7_pct is not None else None for r in ordered], now=now
    )

    table_rows: list[list[str]] = []
    for row, h5_reset, w7_reset in zip(ordered, h5_resets, w7_resets, strict=True):
        name = row.account.name
        marker = ("@" if name == active else " ") + (">" if name == chosen else " ")
        table_rows.append(
            [
                f"{marker} {name}",
                _pct(row.h5_pct),
                h5_reset,
                _pct(row.w7_pct),
                w7_reset,
                _days_left(row.account.effective_expires_at, now_utc),
            ]
        )

    table = _box_table(
        ["Account", "5h", "5h reset", "Week", "Week reset", "Sub"],
        table_rows,
        ["l", "r", "l", "r", "l", "r"],
    )

    lines: list[str] = [
        "Legend: @ = running in this session, > = next pick (rotation), @> = both."
        " Sub = days until subscription end.",
        _status_line(active, chosen),
        "",
    ]
    if fenced:
        lines.append("```")
    lines.append(table)
    if fenced:
        lines.append("```")
    lines.append("")
    lines.extend(_warnings(ordered, active=active, now_utc=now_utc))
    return "\n".join(lines)
