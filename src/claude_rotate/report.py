"""`claude-rotate status --report` — a compact, mobile-friendly account overview.

Where ``render_dashboard`` (dashboard.py) is the rich, colour-bar quota view,
this module renders one narrow, fenced block per account — each with a plain
ASCII progress bar — designed to be relayed verbatim by the bundled Claude Code
skill (see ``skill_assets/account``) and to stay readable on a phone screen
(separate code fences render as separate cards in the chat UI). It answers a
narrower question — *which account is this session on, what are the limits, and
what should I watch out for* — and is therefore a separate, self-contained
renderer rather than another column on the dashboard.

Two markers identify accounts:

* ``@`` — the account this session is currently running on
  (``current-session.json``).
* ``>`` — the account the rotator would pick on the next launch (``chosen``).
* ``@>`` — both, i.e. the session is already on the next pick (no rotation).

Within each account block, every window (``5h`` and ``week``) spans two lines.
The *fact line* aligns the progress bar, the current usage ``%`` and the reset
(absolute clock + compact relative duration). The label-less *forecast sub-line*
beneath it carries the projection: the forecast ``%`` and, once the limit is
crossed before reset, the clock and relative duration at which usage hits 100%.
Both lines share one column grid, so the forecast ``%`` stacks under the current
``%`` and the limit-ETA clock under the reset clock; a shared weekday slot keeps
the clocks aligned even when only the weekly reset lands on another day.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from claude_rotate.config import (
    FORECAST_WINDOW_5H_SECONDS,
    FORECAST_WINDOW_7D_SECONDS,
)
from claude_rotate.dashboard import DashboardRow, compute_forecast, compute_limit_eta

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_WEEKLY_RISK_PCT = 90.0
_FORECAST_RISK_PCT = 100
_EXPIRY_WARN_DAYS = 7
_FALLBACK_MAX_WEEK_PCT = 80.0


class _Cell(NamedTuple):
    """One window's rendered strings for an account card, before column padding.

    ``special`` short-circuits the forecast sub-line: ``"reached"`` (usage already
    ≥100%) or ``"—"`` (no trend yet). When it is ``None`` the sub-line shows the
    ``forecast`` plus the limit ETA (``eta_clock``/``eta_rel``); ``eta_clock`` is
    ``"—"`` when the window resets before the limit is reached.
    """

    label: str
    pct: float | None
    pct_str: str
    reset_clock: str
    reset_rel: str
    special: str | None
    forecast: str | None
    eta_clock: str | None
    eta_rel: str | None


def _render_cards(
    ordered: Sequence[DashboardRow],
    *,
    active: str | None,
    chosen: str | None,
    now: datetime,
    now_utc: datetime,
) -> list[str]:
    """Render each account as its own narrow block (one Markdown fence each).

    A box-drawing table is ~60 columns wide and overflows a phone screen. Each
    account instead becomes a self-contained block: a header carrying the markers,
    name and remaining subscription days, then — per window (``5h`` and ``week``) —
    a *fact line* and a label-less *forecast sub-line*.

    The fact line carries what is true now: the progress bar (same ``█``/``░``
    glyphs as ``dashboard.gradient_bar``, no colour), the current usage %, and the
    reset (absolute clock with a shared weekday slot + a compact relative
    duration). The sub-line beneath carries everything projected: the forecast %
    (``→``-prefixed) and, when the limit is crossed before reset
    (``compute_forecast >= 100``), the clock and relative duration at which usage
    hits 100%. It collapses to ``→XX% —`` when the window resets first, a lone
    ``—`` when there is no trend yet, or ``reached`` once usage is already ≥100%.

    Both line types share one column grid (pct / clock / relative widths span
    both), so the forecast % stacks under the current % and the limit-ETA clock
    under the reset clock. ``build_report`` wraps each returned block in its own
    fence so the chat UI renders them as separate cards.
    """
    label_width = len("week")
    # A label-less sub-line is indented to the fact line's pct column: blank label
    # + blank bar, with the fact line's two-space gaps.
    sub_prefix = f"{'':<{label_width}}  {' ' * _BAR_WIDTH}  "

    def reset_clock(reset_secs: int, *, show_weekday: bool) -> str:
        reset = now + timedelta(seconds=max(reset_secs, 0))
        clock = reset.strftime("%H:%M")
        if show_weekday:
            weekday = WEEKDAYS[reset.weekday()] if reset.date() != now.date() else "   "
            clock = f"{weekday} {clock}"
        return clock

    blocks: list[str] = []
    for row in ordered:
        name = row.account.name
        marker = ("@" if name == active else " ") + (">" if name == chosen else " ")
        head = f"{marker} {name}"
        days = _days_left(row.account.effective_expires_at, now_utc)
        if days != "-":
            head += f" · {days} left"

        specs = (
            ("5h", row.h5_pct, row.h5_reset_secs, FORECAST_WINDOW_5H_SECONDS),
            ("week", row.w7_pct, row.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS),
        )
        # Show a weekday on every clock as soon as any dated reset lands on another
        # day. A limit ETA is always earlier than its own reset, so if every reset
        # is today every ETA is too — the resets alone decide the shared slot.
        show_weekday = any(
            pct is not None and (now + timedelta(seconds=max(secs, 0))).date() != now.date()
            for _, pct, secs, _ in specs
        )

        cells: list[_Cell] = []
        for label, pct, secs, window in specs:
            forecast = compute_forecast(pct, secs, window)
            if pct is not None:
                reset_clk, reset_rel = reset_clock(secs, show_weekday=show_weekday), _rel(secs)
            else:
                reset_clk, reset_rel = "—", ""
            if pct is not None and pct >= 100:
                special, fc_str, eta_clk, eta_rel = "reached", None, None, None
            elif pct is None or pct <= 0 or forecast is None:
                special, fc_str, eta_clk, eta_rel = "—", None, None, None
            else:
                special, fc_str = None, f"→{forecast}%"
                eta = compute_limit_eta(pct, secs, window)
                if eta is not None:
                    eta_clk, eta_rel = reset_clock(eta, show_weekday=show_weekday), _rel(eta)
                else:
                    eta_clk, eta_rel = "—", ""
            cells.append(
                _Cell(
                    label, pct, _pct(pct), reset_clk, reset_rel, special, fc_str, eta_clk, eta_rel
                )
            )

        pw = max(len(s) for c in cells for s in (c.pct_str, c.forecast) if s is not None)
        cw = max(len(s) for c in cells for s in (c.reset_clock, c.eta_clock) if s is not None)
        rel_vals = [s for c in cells for s in (c.reset_rel, c.eta_rel) if s is not None]
        rw = max((len(s) for s in rel_vals), default=0)

        rows_txt = [head]
        for c in cells:
            fact_tail = f"{c.reset_clock:>{cw}} {c.reset_rel:>{rw}}".rstrip()
            fact = f"{c.label:<{label_width}}  {_bar(c.pct)}  {c.pct_str:>{pw}}  {fact_tail}"
            rows_txt.append(fact.rstrip())
            if c.special is not None:
                rows_txt.append(f"{sub_prefix}{c.special:>{pw}}".rstrip())
            else:
                sub_tail = f"{c.eta_clock or '':>{cw}} {c.eta_rel or '':>{rw}}".rstrip()
                rows_txt.append(f"{sub_prefix}{c.forecast or '':>{pw}}  {sub_tail}".rstrip())
        blocks.append("\n".join(rows_txt))
    return blocks


def _pct(value: float | None) -> str:
    return f"{value:g}%" if value is not None else "N/A"


_BAR_FILLED = "█"  # same glyphs as dashboard.gradient_bar (plain, no colour here)
_BAR_EMPTY = "░"
_BAR_WIDTH = 5  # half-width keeps each account line within a phone's screen width


def _bar(pct: float | None, width: int = _BAR_WIDTH) -> str:
    """Plain (colourless) progress bar; ``None`` (no data) renders all-empty."""
    if pct is None:
        return _BAR_EMPTY * width
    filled = round(max(0.0, min(100.0, pct)) / 100 * width)
    return _BAR_FILLED * filled + _BAR_EMPTY * (width - filled)


def _rel(reset_secs: int) -> str:
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

    cards = _render_cards(ordered, active=active, chosen=chosen, now=now, now_utc=now_utc)

    lines: list[str] = [
        "Legend: @ = running in this session, > = next pick (rotation), @> = both."
        " Sub = days until subscription end.",
        _status_line(active, chosen),
        "",
    ]
    for card in cards:
        if fenced:
            lines.append("```")
        lines.append(card)
        if fenced:
            lines.append("```")
        lines.append("")  # blank line between blocks (and before the warnings)
    lines.extend(_warnings(ordered, active=active, now_utc=now_utc))
    return "\n".join(lines)
