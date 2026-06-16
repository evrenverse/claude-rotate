"""`claude-rotate status --report` — a compact, mobile-friendly account overview.

Where ``render_dashboard`` (dashboard.py) is the rich, colour-bar quota view,
this module renders one narrow, fenced block per account — each with a plain
ASCII progress bar — designed to be relayed verbatim by the bundled Claude Code
skill (see ``skill_assets/account``) and to stay readable on a phone screen
(separate code fences render as separate cards in the chat UI). It answers a
narrower question — *which account is this session on, what are the limits, and
what should I watch out for* — and is therefore a separate, self-contained
renderer rather than another column on the dashboard. Quota semantics
(forecasts, risk thresholds, wording) are shared with the dashboard via
``claude_rotate.insights``.

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
from claude_rotate.dashboard import DashboardRow, session_indicator
from claude_rotate.insights import (
    clock_at,
    compute_forecast,
    compute_limit_eta,
    days_left,
    expiry_horizon,
    pct_str,
    rel_duration,
    status_line,
    warning_messages,
)


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
    capped: bool = False


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

    blocks: list[str] = []
    for row in ordered:
        name = row.account.name
        marker = ("@" if name == active else " ") + (">" if name == chosen else " ")
        head = f"{marker} {name}"
        days = days_left(row.account.effective_expires_at, now_utc)
        if days != "-":
            head += f" · {days} left"
        if row.account.disabled:
            head += " · disabled"
        indicator = session_indicator(row.session_load)
        if indicator:
            head += f" · {indicator}"

        specs = (
            ("5h", row.h5_pct, row.h5_reset_secs, FORECAST_WINDOW_5H_SECONDS),
            ("week", row.w7_pct, row.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS),
        )

        # Show a weekday on every clock as soon as any dated reset lands on another
        # day. A limit ETA is always earlier than its own reset, so if every reset
        # is today every ETA is too — the resets alone decide the shared slot.
        # When the subscription expires before a window resets, use the horizon
        # (expiry offset) instead of the full reset offset for the weekday check.
        def _lands_on_other_day(secs: int) -> bool:
            return (now + timedelta(seconds=max(secs, 0))).date() != now.date()

        show_weekday = any(
            pct is not None
            and _lands_on_other_day(
                expiry_horizon(row.account.effective_expires_at, secs, now_utc) or secs
            )
            for _, pct, secs, _ in specs
        )

        cells: list[_Cell] = []
        for label, pct, secs, window in specs:
            horizon_arg = expiry_horizon(row.account.effective_expires_at, secs, now_utc)
            capped = horizon_arg is not None
            forecast = compute_forecast(pct, secs, window, horizon_arg)
            if pct is not None:
                hz = horizon_arg if horizon_arg is not None else secs
                reset_clk = clock_at(now, hz, show_weekday=show_weekday)
                reset_rel = rel_duration(hz)
            else:
                reset_clk, reset_rel = "—", ""
            if pct is not None and pct >= 100:
                special, fc_str, eta_clk, eta_rel = "reached", None, None, None
            elif pct is None or pct <= 0 or forecast is None:
                special, fc_str, eta_clk, eta_rel = "—", None, None, None
            else:
                special, fc_str = None, f"→{forecast}%"
                eta = compute_limit_eta(pct, secs, window, horizon_arg)
                if eta is not None:
                    eta_clk = clock_at(now, eta, show_weekday=show_weekday)
                    eta_rel = rel_duration(eta)
                else:
                    eta_clk, eta_rel = "—", ""
            cells.append(
                _Cell(
                    label,
                    pct,
                    pct_str(pct),
                    reset_clk,
                    reset_rel,
                    special,
                    fc_str,
                    eta_clk,
                    eta_rel,
                    capped,
                )
            )

        pw = max(len(s) for c in cells for s in (c.pct_str, c.forecast) if s is not None)
        cw = max(len(s) for c in cells for s in (c.reset_clock, c.eta_clock) if s is not None)
        rel_vals = [s for c in cells for s in (c.reset_rel, c.eta_rel) if s is not None]
        rw = max((len(s) for s in rel_vals), default=0)

        rows_txt = [head]
        for c in cells:
            fact_tail = f"{c.reset_clock:>{cw}} {c.reset_rel:>{rw}}".rstrip()
            if c.capped:
                # Append after the right-justified clock/rel field so the wide ⌛
                # glyph stays outside the column grid and keeps the cards aligned.
                fact_tail += " ⌛"
            fact = f"{c.label:<{label_width}}  {_bar(c.pct)}  {c.pct_str:>{pw}}  {fact_tail}"
            rows_txt.append(fact.rstrip())
            if c.special is not None:
                rows_txt.append(f"{sub_prefix}{c.special:>{pw}}".rstrip())
            else:
                sub_tail = f"{c.eta_clock or '':>{cw}} {c.eta_rel or '':>{rw}}".rstrip()
                rows_txt.append(f"{sub_prefix}{c.forecast or '':>{pw}}  {sub_tail}".rstrip())
        blocks.append("\n".join(rows_txt))
    return blocks


_BAR_FILLED = "█"  # same glyphs as dashboard.gradient_bar (plain, no colour here)
_BAR_EMPTY = "░"
_BAR_WIDTH = 5  # half-width keeps each account line within a phone's screen width


def _bar(pct: float | None, width: int = _BAR_WIDTH) -> str:
    """Plain (colourless) progress bar; ``None`` (no data) renders all-empty."""
    if pct is None:
        return _BAR_EMPTY * width
    filled = round(max(0.0, min(100.0, pct)) / 100 * width)
    return _BAR_FILLED * filled + _BAR_EMPTY * (width - filled)


def _warnings(rows: Sequence[DashboardRow], *, active: str | None, now_utc: datetime) -> list[str]:
    """Action-needed lines (re-login / expiring subscription); empty when none.

    Quota-risk warnings and the fallback recommendation were intentionally
    dropped — the per-account cards already carry usage and forecast.
    """
    msgs = warning_messages(rows, active=active, now_utc=now_utc)
    if not msgs:
        return []
    return ["⚠️ Warnings:", *[f"- {msg}" for msg in msgs]]


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
        status_line(active, chosen),
        "",
    ]
    for card in cards:
        if fenced:
            lines.append("```")
        lines.append(card)
        if fenced:
            lines.append("```")
        lines.append("")  # blank line between account blocks (and before warnings)
    lines.extend(_warnings(ordered, active=active, now_utc=now_utc))
    return "\n".join(lines)
