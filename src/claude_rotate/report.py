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
(absolute clock + compact relative duration). A ``~`` before the ``%`` marks a
cached last-known value (the live fetch failed) — same convention as the
dashboard's cache-served rows. The label-less *forecast sub-line*
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
from claude_rotate.providers import ProviderQuota, ProviderWindow


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
    # Scoped labels (e.g. "fable") can be wider than "week"; share one width
    # across all cards so every fact line's bar column stays aligned.
    label_width = max([len("week"), *(len(s.label) for row in ordered for s in row.w7_scoped)])
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
            (
                "5h",
                row.h5_pct,
                row.h5_reset_secs,
                FORECAST_WINDOW_5H_SECONDS,
                row.h5_rate_per_sec,
                row.from_cache,
            ),
            (
                "week",
                row.w7_pct,
                row.w7_reset_secs,
                FORECAST_WINDOW_7D_SECONDS,
                row.w7_rate_per_sec,
                row.from_cache,
            ),
            # Model-scoped weekly windows (e.g. Fable's own cap). No burn-rate
            # history is tracked for these, so the forecast falls back to the
            # average-pace projection (rate=None). A scoped value can be a
            # cache backfill even when the row is live — mark it stale then.
            *(
                (
                    s.label,
                    s.pct,
                    s.reset_secs,
                    FORECAST_WINDOW_7D_SECONDS,
                    None,
                    row.from_cache or s.stale,
                )
                for s in row.w7_scoped
            ),
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
            for _, pct, secs, _, _, _ in specs
        )

        cells: list[_Cell] = []
        for label, pct, secs, window, rate, stale in specs:
            horizon_arg = expiry_horizon(row.account.effective_expires_at, secs, now_utc)
            capped = horizon_arg is not None
            forecast = compute_forecast(pct, secs, window, horizon_arg, rate)
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
                eta = compute_limit_eta(pct, secs, window, horizon_arg, rate)
                if eta is not None:
                    eta_clk = clock_at(now, eta, show_weekday=show_weekday)
                    eta_rel = rel_duration(eta)
                else:
                    eta_clk, eta_rel = "—", ""
            # ``~`` = cached last-known value (live fetch failed) — same
            # marker the dashboard uses for cache-served rows.
            marked_pct = f"~{pct_str(pct)}" if stale and pct is not None else pct_str(pct)
            cells.append(
                _Cell(
                    label,
                    pct,
                    marked_pct,
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
    providers: Sequence[ProviderQuota] = (),
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
        " Sub = days until subscription end. ~% = last known value (live fetch failed).",
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
    lines.extend(_provider_block(providers, now=now, fenced=fenced))
    return "\n".join(lines)


def _provider_block(
    providers: Sequence[ProviderQuota], *, now: datetime, fenced: bool
) -> list[str]:
    """The other subscriptions, as one compact card; empty when there are none.

    One card rather than one per provider: these rows carry two numbers each,
    and splitting them would cost more screen than it buys on a phone. Each
    window spans two lines like the account cards — fact line, then the
    average-pace projection (these providers keep no burn-rate history).
    """
    if not providers:
        return []
    card: list[str] = []
    for quota in providers:
        name = quota.provider
        if quota.account and quota.account != quota.provider:
            name = f"{name} · {quota.account}"
        card.append(name)
        if not quota.windows:
            card.append(f"  {quota.note or 'no data'}")
            continue
        for window in quota.windows:
            card.extend(_provider_window_lines(window, now=now))
        if quota.note:
            card.append(f"  {quota.note}")

    lines = ["other providers", ""]
    if fenced:
        lines.append("```")
    lines.append("\n".join(card))
    if fenced:
        lines.append("```")
    lines.append("")
    return lines


_PROVIDER_WINDOW_SECONDS = {
    "5h": FORECAST_WINDOW_5H_SECONDS,
    "week": FORECAST_WINDOW_7D_SECONDS,
}


def _provider_window_lines(window: ProviderWindow, *, now: datetime) -> list[str]:
    """Fact line plus projection sub-line for one provider window."""
    secs = window.reset_secs
    window_secs = _PROVIDER_WINDOW_SECONDS.get(window.label, secs)
    reset = f"{clock_at(now, secs, show_weekday=True)} {rel_duration(secs)}" if secs else ""
    fact = f"  {window.label:<5}{_bar(window.used_pct)}{window.used_pct:>5.0f}%  {reset}".rstrip()

    forecast = compute_forecast(window.used_pct, secs, window_secs)
    if window.used_pct >= 100:
        sub = "reached"
    elif window.used_pct <= 0 or forecast is None:
        sub = "—"
    else:
        eta = compute_limit_eta(window.used_pct, secs, window_secs)
        sub = f"→{forecast}%"
        if eta is not None:
            sub += f"  {clock_at(now, eta, show_weekday=True)} {rel_duration(eta)}"
    # Indent to the usage column so the projection stacks under the percent.
    return [fact, f"  {'':<5}{' ' * _BAR_WIDTH}{sub:>5}".rstrip()]
