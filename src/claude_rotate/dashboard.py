"""Status dashboard rendering with rich.

``render_dashboard`` is responsive: gradient bars grow with the terminal,
relative durations are dropped first when space gets tight, and below
``_CARDS_MAX_WIDTH`` the four columns fold into a single-column bordered table
— one ruled, framed card per account, with the header and both windows stacked
vertically so a phone-width terminal still reads as a table. Accounts that
cannot be picked right now — a
window at/over the limit or an expired subscription — render flattened to
uniform grey (``is_unusable`` + ``_greyed``) so the eye skips them. Each window
(5h / week) renders a *fact line* (bar, usage %, reset clock + relative
duration) and a dimmed *forecast sub-line* (projected % at reset and, when
the limit is crossed before reset, the clock at which usage hits 100%).
Shared quota semantics (forecasts, warnings, wording) live in
``claude_rotate.insights`` and are reused by the ``--report`` renderer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from claude_rotate.accounts import Account
from claude_rotate.config import (
    FORECAST_WINDOW_5H_SECONDS,
    FORECAST_WINDOW_7D_SECONDS,
    HEADROOM_PERCENT,
    STALE_METADATA_WARN_DAYS,
)
from claude_rotate.insights import (
    clock_at,
    compute_forecast,
    compute_limit_eta,
    expiry_horizon,
    plan_label,
    rel_duration,
    status_line,
    warning_messages,
)
from claude_rotate.sessions import SessionLoad

__all__ = [
    "DashboardRow",
    "compact_one_liner",
    "compute_forecast",
    "compute_limit_eta",
    "fmt_sub_expiry",
    "forecast_enabled",
    "gradient_bar",
    "is_unusable",
    "render_dashboard",
    "render_stale_footer",
    "session_indicator",
    "status_json",
]

_FILLED = "█"
_EMPTY = "░"

# Accounts with no known subscription expiry sort after every dated one.
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _by_expiry(rows: list[DashboardRow]) -> list[DashboardRow]:
    """Order rows so the earliest-expiring subscription is first.

    Accounts with no known expiry sort last; ties break on name for a stable
    order. Disabled accounts are not special-cased — they sort by expiry like
    any other and render greyed-out via ``is_unusable``.
    """
    return sorted(
        rows,
        key=lambda r: (r.account.effective_expires_at or _FAR_FUTURE, r.account.name),
    )


def _interp(a: int, b: int, t: float) -> int:
    return int(a * (1 - t) + b * t)


def _color_at(i: int, width: int) -> str:
    """Per-cell gradient blue → amber → orange → red.

    Same math as the existing claude-rotate Bash statusline so the two UIs
    look identical.
    """
    p = 0.0 if width <= 1 else i / (width - 1)
    if p < 1 / 3:
        t = p * 3
        r, g, b = _interp(91, 245, t), _interp(158, 200, t), _interp(245, 91, t)
    elif p < 2 / 3:
        t = (p - 1 / 3) * 3
        r, g, b = _interp(245, 255, t), _interp(200, 140, t), _interp(91, 66, t)
    else:
        t = (p - 2 / 3) * 3
        r, g, b = _interp(255, 245, t), _interp(140, 91, t), _interp(66, 91, t)
    return f"rgb({r},{g},{b})"


def gradient_bar(pct: float, width: int = 12) -> Text:
    """Render a fixed-width bar with per-cell gradient fill."""
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    bar = Text()
    for i in range(width):
        if i < filled:
            bar.append(_FILLED, style=_color_at(i, width))
        else:
            bar.append(_EMPTY, style="grey50")
    return bar


@dataclass(frozen=True)
class DashboardRow:
    account: Account
    h5_pct: float | None
    w7_pct: float | None
    h5_reset_secs: int
    w7_reset_secs: int
    from_cache: bool = False
    status: str = "ok"  # "ok" | "relogin" | "rate_limited" | "sub_canceled" | "no_data"
    note: str = ""
    session_load: SessionLoad | None = None


_EXPIRY_GRADIENT_DAYS = 30


def _expiry_color(days: int, width: int = 12) -> str:
    """Urgency colour from the same blue→amber→red gradient as the bars.

    Days are mapped onto the gradient so 0d sits at the full-red endpoint
    and ``_EXPIRY_GRADIENT_DAYS`` days (≈ one Max billing cycle) sits at
    the blue/teal start. In between, the colour escalates linearly — the
    closer to the end date, the redder the cell.
    """
    if days <= 0:
        return _color_at(width - 1, width)  # reddest
    if days >= _EXPIRY_GRADIENT_DAYS:
        return _color_at(0, width)  # coolest
    urgency = (_EXPIRY_GRADIENT_DAYS - days) / _EXPIRY_GRADIENT_DAYS
    idx = min(width - 1, max(0, round(urgency * width) - 1))
    return _color_at(idx, width)


def fmt_sub_expiry(
    expires_at: datetime | None,
    status: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return (text, rich-style) for the subscription column.

    Days render as ``Nd``; when already past, ``Nh`` until cutoff. Colour
    is a per-day gradient from the same palette as the bars (blue → amber
    → red). A ``⚠`` prefix is added for canceled/past_due subscriptions
    inside the 10-day window to make the impending cutoff extra visible.
    Empty when we have no information (CI-installed account).
    """
    if expires_at is None:
        return "", ""
    now = now or datetime.now(UTC)
    delta = expires_at - now
    days = delta.days
    colour = _expiry_color(days)
    if days <= 0:
        hours = max(0, int(delta.total_seconds() // 3600))
        return f"{hours}h", colour
    text = f"{days}d"
    if status in ("canceled", "past_due") and days <= 10:
        text = f"⚠ {text}"
    return text, colour


def _pct_color(pct: float | None, width: int = 12) -> str:
    """Return the gradient colour at the last filled cell position."""
    if pct is None:
        return "grey50"
    filled = round(max(0.0, min(100.0, pct)) / 100 * width)
    if filled <= 0:
        return "grey50"
    return _color_at(filled - 1, width)


def forecast_enabled() -> bool:
    """Whether the status dashboard renders the →XX% forecast sub-lines.

    On by default; ``CLAUDE_ROTATE_FORECAST=0`` disables it. Mirrors the toggle
    in the separate (external) Bash statusline project so the two UIs agree.
    """
    return os.environ.get("CLAUDE_ROTATE_FORECAST", "1") != "0"


def session_indicator(load: SessionLoad | None) -> str:
    """Compact 'N active · M idle' string; empty when nothing is open."""
    if load is None or load.open == 0:
        return ""
    parts: list[str] = []
    if load.active:
        parts.append(f"{load.active} active")
    if load.idle:
        parts.append(f"{load.idle} idle")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Responsive dashboard
# ---------------------------------------------------------------------------

_BAR_MIN = 8
_BAR_MAX = 20
_CARDS_MAX_WIDTH = 76  # below this terminal width, fold the table into cards
_CARD_BAR_WIDTH = 10
_ETA_URGENT_SECS = 3600  # limit-ETA under an hour renders red, not dim

_STATUS_LABELS = {
    "relogin": ("RELOGIN", "red"),
    "rate_limited": ("LIMITED", "yellow"),
    "sub_canceled": ("CANCELED", "red"),
}


_UNUSABLE_STYLE = "grey35"


def _greyed(t: Text) -> Text:
    """Flatten a styled cell to uniform dark grey — unusable rows lose their colours.

    A plain row ``style=`` is not enough: per-span colours (gradient bars,
    pct/forecast colours) sit above the row style and would survive, leaving the
    row merely darkened instead of visibly out of rotation.
    """
    return Text(t.plain, style=_UNUSABLE_STYLE)


def is_unusable(row: DashboardRow, *, now: datetime) -> bool:
    """Whether this account cannot be picked right now — drives the dimmed row.

    Mirrors ``selection.is_usable`` (a window at/over ``HEADROOM_PERCENT`` takes
    the account out of rotation) and additionally treats an expired subscription
    — or a manually ``disabled`` account — as unusable. Error-status rows
    (relogin/canceled) keep their loud coloured labels instead — those need
    action, not de-emphasis.
    """
    if row.account.disabled:
        return True
    if row.status == "ok":
        if row.h5_pct is not None and row.h5_pct >= HEADROOM_PERCENT:
            return True
        if row.w7_pct is not None and row.w7_pct >= HEADROOM_PERCENT:
            return True
    expires_at = row.account.effective_expires_at
    return expires_at is not None and expires_at <= now


@dataclass(frozen=True)
class _WindowCell:
    """One window's pre-rendered strings for one account row."""

    pct: float | None
    pct_str: str
    clock: str
    rel: str
    forecast: int | None
    fc_str: str
    eta_secs: int | None
    eta_clock: str
    eta_rel: str
    capped: bool = False


_NA_CELL = _WindowCell(None, "N/A", "", "", None, "", None, "", "")


def _window_cells(
    rows: list[DashboardRow],
    window: str,
    window_secs: int,
    *,
    now_local: datetime,
    show_forecast: bool,
) -> list[_WindowCell]:
    """Build one column of cells; the weekday slot is shared per column."""
    datas: list[tuple[float | None, int, bool, int | None]] = []
    for r in rows:
        pct = r.h5_pct if window == "5h" else r.w7_pct
        secs = r.h5_reset_secs if window == "5h" else r.w7_reset_secs
        horizon_arg = expiry_horizon(r.account.effective_expires_at, secs, now_local)
        datas.append((pct if r.status == "ok" else None, secs, r.from_cache, horizon_arg))

    def lands_on_other_day(secs: int) -> bool:
        return (now_local + timedelta(seconds=max(secs, 0))).date() != now_local.date()

    show_weekday = any(
        pct is not None
        and (
            lands_on_other_day(horizon_arg if horizon_arg is not None else secs)
            or (
                show_forecast
                and (eta := compute_limit_eta(pct, secs, window_secs, horizon_arg)) is not None
                and lands_on_other_day(eta)
            )
        )
        for pct, secs, _, horizon_arg in datas
    )

    cells: list[_WindowCell] = []
    for pct, secs, from_cache, horizon_arg in datas:
        if pct is None:
            cells.append(_NA_CELL)
            continue
        capped = horizon_arg is not None
        horizon = horizon_arg if capped else secs
        forecast = compute_forecast(pct, secs, window_secs, horizon_arg) if show_forecast else None
        eta = compute_limit_eta(pct, secs, window_secs, horizon_arg) if show_forecast else None
        prefix = "~" if from_cache else ""
        cells.append(
            _WindowCell(
                pct=pct,
                pct_str=f"{prefix}{pct:g}%",
                clock=clock_at(now_local, horizon, show_weekday=show_weekday),
                rel=rel_duration(horizon),
                forecast=forecast,
                fc_str=f"→{forecast}%" if forecast is not None else "",
                eta_secs=eta,
                eta_clock=(
                    clock_at(now_local, eta, show_weekday=show_weekday) if eta is not None else ""
                ),
                eta_rel=rel_duration(eta) if eta is not None else "",
                capped=capped,
            )
        )
    return cells


def _col_widths(cells: list[_WindowCell], *, include_rel: bool) -> tuple[int, int, int]:
    """(pct, clock, rel) column widths shared by fact line and sub-line."""
    pw = max((len(s) for c in cells for s in (c.pct_str, c.fc_str) if s), default=3)
    cw = max((len(s) for c in cells for s in (c.clock, c.eta_clock) if s), default=0)
    rw = 0
    if include_rel:
        rw = max((len(s) for c in cells for s in (c.rel, c.eta_rel) if s), default=0)
    return pw, cw, rw


def _window_text(c: _WindowCell, *, bar_w: int, pw: int, cw: int, rw: int, label: str = "") -> Text:
    """Fact line + optional forecast sub-line for one window cell."""
    t = Text()
    if label:
        t.append(label)
    if c.pct is None:
        t.append("N/A", style="grey50")
        return t
    t.append_text(gradient_bar(c.pct, width=bar_w))
    t.append("  ")
    t.append(f"{c.pct_str:>{pw}}", style=_pct_color(c.pct, width=bar_w))
    if cw and c.clock:
        t.append("  ")
        t.append(f"{c.clock:>{cw}}", style="dim" if c.capped else "")
        if rw and c.rel:
            t.append(" ")
            t.append(f"{c.rel:>{rw}}", style="dim")
        if c.capped:
            # The clock shows the subscription expiry, not the window reset.
            t.append(" ⌛", style="dim")
    if not (c.fc_str or c.eta_clock):
        return t
    t.append("\n")
    t.append(" " * (len(label) + bar_w + 2))
    fc_style = _pct_color(float(c.forecast), width=bar_w) if c.forecast else "grey50"
    t.append(f"{c.fc_str:>{pw}}", style=fc_style)
    if cw and c.eta_clock:
        eta_style = "red" if c.eta_secs is not None and c.eta_secs < _ETA_URGENT_SECS else "dim"
        t.append("  ")
        t.append(f"{c.eta_clock:>{cw}}", style=eta_style)
        if rw and c.eta_rel:
            t.append(" ")
            t.append(f"{c.eta_rel:>{rw}}", style=eta_style)
    return t


def _label_text(row: DashboardRow, *, chosen: str | None, active: str | None) -> Text:
    """Two-line account label: markers + name, plan badge dimmed beneath."""
    name = row.account.name
    is_active = name == active
    if row.account.disabled:
        # Manually out of rotation — neither chosen nor pinnable.
        m2, m2_style = "⊘", "grey50"
    elif row.account.pinned:
        # Pinned wins over chosen: a pinned account is always chosen, the ★
        # carries more information.
        m2, m2_style = "★", "yellow"
    elif name == chosen:
        m2, m2_style = ">", "green"
    else:
        m2, m2_style = " ", ""
    t = Text()
    t.append("@" if is_active else " ", style="cyan bold")
    t.append(m2, style=m2_style)
    t.append(f" {name}", style="bold" if is_active else "")
    plan = plan_label(row.account.plan)
    sub = f"{plan} · disabled" if (plan and row.account.disabled) else (plan or "")
    if not sub and row.account.disabled:
        sub = "disabled"
    if sub:
        t.append(f"\n   {sub}", style="dim")
    indicator = session_indicator(row.session_load)
    if indicator:
        t.append(f"\n   {indicator}", style="dim cyan")
    return t


def _sub_text(row: DashboardRow, *, now: datetime) -> Text:
    """Two-line subscription cell: coloured days left, absolute date beneath."""
    txt, style = fmt_sub_expiry(
        row.account.effective_expires_at,
        status=row.account.subscription_status,
        now=now,
    )
    t = Text()
    if not txt:
        return t
    t.append(txt, style=style or "")
    expires_at = row.account.effective_expires_at
    if expires_at is not None:
        t.append("\n")
        t.append(expires_at.astimezone().strftime("%d %b"), style="dim")
    return t


def _status_text(row: DashboardRow) -> Text:
    label, style = _STATUS_LABELS[row.status]
    t = Text()
    t.append(label, style=style)
    if row.note:
        t.append(f"  {row.note}", style="dim")
    return t


def _render_table(
    rows: list[DashboardRow],
    *,
    console: Console,
    chosen: str | None,
    active: str | None,
    now: datetime,
    now_local: datetime,
    show_forecast: bool,
) -> bool:
    """Render the wide table; ``False`` when even the rel-less layout is too wide."""
    cells5 = _window_cells(
        rows, "5h", FORECAST_WINDOW_5H_SECONDS, now_local=now_local, show_forecast=show_forecast
    )
    cells7 = _window_cells(
        rows, "week", FORECAST_WINDOW_7D_SECONDS, now_local=now_local, show_forecast=show_forecast
    )
    labels = [_label_text(r, chosen=chosen, active=active) for r in rows]
    subs = [_sub_text(r, now=now) for r in rows]
    label_w = max((max(len(ln) for ln in lbl.plain.split("\n")) for lbl in labels), default=0)
    sub_w = max((max(len(ln) for ln in s.plain.split("\n")) for s in subs if s.plain), default=0)
    sub_w = max(sub_w, len("sub"))

    for include_rel in (True, False):
        pw5, cw5, rw5 = _col_widths(cells5, include_rel=include_rel)
        pw7, cw7, rw7 = _col_widths(cells7, include_rel=include_rel)

        def text_w(pw: int, cw: int, rw: int) -> int:
            return 2 + pw + ((2 + cw) if cw else 0) + ((1 + rw) if rw else 0)

        # Bordered-table chrome: 5 vertical rules + 4 columns x 2 padding cells.
        chrome = 5 + 4 * 2
        overhead = label_w + sub_w + chrome + text_w(pw5, cw5, rw5) + text_w(pw7, cw7, rw7)
        slack = (console.width - overhead) // 2
        if slack < _BAR_MIN:
            continue
        bar_w = min(slack, _BAR_MAX)

        table = Table(
            box=box.ROUNDED,
            show_lines=True,  # rule between accounts — each row reads as its own band
            padding=(0, 1),
            border_style="dim",
            header_style="bold",
        )
        table.add_column("", no_wrap=True)
        table.add_column("5h", no_wrap=True)
        table.add_column("week", no_wrap=True)
        table.add_column("sub", no_wrap=True, justify="right")
        for row, lbl, c5, c7, sub in zip(rows, labels, cells5, cells7, subs, strict=True):
            unusable = is_unusable(row, now=now)
            if row.status in _STATUS_LABELS:
                # The status label stays loud even when the row is greyed out —
                # it asks for action.
                table.add_row(
                    _greyed(lbl) if unusable else lbl,
                    Text("N/A", style="grey50"),
                    _status_text(row),
                    _greyed(sub) if unusable else sub,
                )
                continue
            t5 = _window_text(c5, bar_w=bar_w, pw=pw5, cw=cw5, rw=rw5)
            t7 = _window_text(c7, bar_w=bar_w, pw=pw7, cw=cw7, rw=rw7)
            if unusable:
                table.add_row(_greyed(lbl), _greyed(t5), _greyed(t7), _greyed(sub))
            else:
                table.add_row(lbl, t5, t7, sub)
        console.print()
        console.print(table)
        return True
    return False


def _card_text(
    row: DashboardRow,
    c5: _WindowCell,
    c7: _WindowCell,
    *,
    chosen: str | None,
    active: str | None,
    now: datetime,
    unusable: bool,
) -> Text:
    """One account's card body — header line plus stacked window lines.

    Returned as a single (multi-line) ``Text`` so it drops into one cell of the
    compact bordered table. Greying mirrors the wide table: an unusable account
    flattens header and window lines to grey, but a loud error-status label
    stays coloured because it asks for action.
    """
    header = Text()
    name = row.account.name
    is_active = name == active
    header.append("@" if is_active else " ", style="cyan bold")
    if row.account.disabled:
        header.append("⊘", style="grey50")
    elif row.account.pinned:
        header.append("★", style="yellow")
    else:
        header.append(">" if name == chosen else " ", style="green")
    header.append(f" {name}", style="bold" if is_active else "")
    plan = plan_label(row.account.plan)
    if plan:
        header.append(f" · {plan}", style="dim")
    if row.account.disabled:
        header.append(" · disabled", style="dim")
    exp_txt, exp_style = fmt_sub_expiry(
        row.account.effective_expires_at,
        status=row.account.subscription_status,
        now=now,
    )
    if exp_txt:
        header.append(" · ", style="dim")
        header.append(exp_txt, style=exp_style or "")
        expires_at = row.account.effective_expires_at
        if expires_at is not None:
            header.append(f" ({expires_at.astimezone().strftime('%d %b')})", style="dim")

    card = Text()
    card.append_text(_greyed(header) if unusable else header)

    if row.status in _STATUS_LABELS:
        # Loud status label even on a greyed card — it asks for action.
        card.append("\n")
        card.append_text(_status_text(row))
        return card

    both = [c5, c7]
    pw = max((len(s) for c in both for s in (c.pct_str, c.fc_str) if s), default=3)
    cw = max((len(s) for c in both for s in (c.clock, c.eta_clock) if s), default=0)
    rw = max((len(s) for c in both for s in (c.rel, c.eta_rel) if s), default=0)
    for label, cell in (("5h    ", c5), ("week  ", c7)):
        line = _window_text(cell, bar_w=_CARD_BAR_WIDTH, pw=pw, cw=cw, rw=rw, label=label)
        card.append("\n")
        card.append_text(_greyed(line) if unusable else line)
    return card


def _render_cards(
    rows: list[DashboardRow],
    *,
    console: Console,
    chosen: str | None,
    active: str | None,
    now: datetime,
    now_local: datetime,
    show_forecast: bool,
) -> None:
    """Narrow-terminal layout: one bordered, ruled card per account.

    Same chrome as the wide table (rounded border + a horizontal rule between
    accounts) so a phone-width terminal still reads as a table — but the four
    columns stack vertically inside a single cell, so each account's header and
    both window lines fit the narrow width.
    """
    cells5 = _window_cells(
        rows, "5h", FORECAST_WINDOW_5H_SECONDS, now_local=now_local, show_forecast=show_forecast
    )
    cells7 = _window_cells(
        rows, "week", FORECAST_WINDOW_7D_SECONDS, now_local=now_local, show_forecast=show_forecast
    )
    table = Table(
        box=box.ROUNDED,
        show_lines=True,  # rule between accounts — each card reads as its own band
        show_header=False,
        padding=(0, 1),
        border_style="dim",
    )
    table.add_column("", no_wrap=True)
    for row, c5, c7 in zip(rows, cells5, cells7, strict=True):
        unusable = is_unusable(row, now=now)
        table.add_row(
            _card_text(row, c5, c7, chosen=chosen, active=active, now=now, unusable=unusable)
        )
    console.print()
    console.print(table)


def render_dashboard(
    rows: list[DashboardRow],
    *,
    chosen: str | None,
    console: Console,
    active: str | None = None,
    now: datetime | None = None,
    show_forecast: bool = True,
) -> None:
    now = now or datetime.now(UTC)
    now_local = now.astimezone()

    # Always order the table by subscription expiry, earliest first.
    rows = _by_expiry(rows)

    console.print()
    console.print(Text(status_line(active, chosen), style="dim"))

    kwargs: dict[str, Any] = dict(
        console=console,
        chosen=chosen,
        active=active,
        now=now,
        now_local=now_local,
        show_forecast=show_forecast,
    )
    if console.width < _CARDS_MAX_WIDTH or not _render_table(rows, **kwargs):
        _render_cards(rows, **kwargs)

    _render_action_footer(rows, console=console, active=active, now_utc=now)


def _render_action_footer(
    rows: list[DashboardRow],
    *,
    console: Console,
    active: str | None,
    now_utc: datetime,
) -> None:
    """Action-needed warnings (re-login / expiring subscription); silent otherwise.

    Quota-usage risk and the fallback recommendation are intentionally omitted —
    the per-account bars already show usage; only actionable signals remain.
    """
    msgs = warning_messages(rows, active=active, now_utc=now_utc)
    if not msgs:
        return
    console.print()
    for msg in msgs:
        line = Text(" ⚠ ", style="yellow")
        line.append(msg)
        console.print(line)


# ---------------------------------------------------------------------------
# Stale-metadata footer, compact non-TTY one-liner, status JSON
# ---------------------------------------------------------------------------


def render_stale_footer(
    rows: list[DashboardRow],
    *,
    console: Console,
    now: datetime | None = None,
) -> None:
    """Warn if any OAuth account has not been refreshed for >STALE_METADATA_WARN_DAYS."""
    now = now or datetime.now(UTC)
    warnings: list[tuple[str, int]] = []
    for row in rows:
        acct = row.account
        if acct.refresh_token is None:
            # CI account — no refresh_token, staleness check doesn't apply
            continue
        last = acct.metadata_refreshed_at
        if last is None:
            warnings.append((acct.name, -1))
            continue
        age_days = (now - last).days
        if age_days > STALE_METADATA_WARN_DAYS:
            warnings.append((acct.name, age_days))
    if not warnings:
        return
    console.print()
    for name, days in warnings:
        age_str = f"{days}d" if days >= 0 else "never refreshed"
        console.print(
            f"  [yellow]⚠[/]  {name}: not refreshed for {age_str}"
            " — refresh_token may be invalidated soon."
        )
    console.print("      Run any [bold]claude-rotate[/] command to trigger auto-refresh.")


def compact_one_liner(row: DashboardRow) -> str:
    """Single-line stderr summary for non-TTY runs."""
    plan_label = row.account.plan
    h5 = f"{row.h5_pct:g}%" if row.h5_pct is not None else "N/A"
    w7 = f"{row.w7_pct:g}%" if row.w7_pct is not None else "N/A"
    return f"→ {row.account.name} ({plan_label}, 5h {h5}, w7 {w7})"


def status_json(
    rows: list[DashboardRow], *, chosen: str | None, active: str | None = None
) -> dict[str, Any]:
    return {
        "chosen": chosen,
        "active": active,
        "accounts": [
            {
                "name": r.account.name,
                "label": r.account.label,
                "plan": r.account.plan,
                "email": r.account.email,
                "h5_pct": r.h5_pct,
                "w7_pct": r.w7_pct,
                "h5_reset_secs": r.h5_reset_secs,
                "w7_reset_secs": r.w7_reset_secs,
                # Always emitted (data, not display): the CLAUDE_ROTATE_FORECAST toggle
                # only suppresses the human dashboard, never the machine-readable JSON.
                "h5_forecast_pct": compute_forecast(
                    r.h5_pct, r.h5_reset_secs, FORECAST_WINDOW_5H_SECONDS
                ),
                "w7_forecast_pct": compute_forecast(
                    r.w7_pct, r.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS
                ),
                "status": r.status,
                "note": r.note,
                "from_cache": r.from_cache,
                "disabled": r.account.disabled,
                "sessions": (
                    {"active": r.session_load.active, "idle": r.session_load.idle}
                    if r.session_load is not None and r.session_load.open > 0
                    else None
                ),
                "subscription_expires_at": (
                    r.account.effective_expires_at.isoformat()
                    if r.account.effective_expires_at
                    else None
                ),
            }
            for r in rows
        ],
    }
