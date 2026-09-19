"""Status dashboard rendering with rich.

``render_dashboard`` is responsive: gradient bars grow with the terminal,
relative durations are dropped first when space gets tight, and below
``_CARDS_MAX_WIDTH`` the four columns fold into a single-column bordered table
— one ruled, framed card per account, with the header and both windows stacked
vertically so a phone-width terminal still reads as a table. Accounts that
cannot be picked right now — a
window at/over the limit or an expired subscription — render flattened to
uniform grey (``is_unusable`` + ``_greyed``) so the eye skips them. Each window
(5h / week, plus any model-scoped weekly window such as Fable's own cap,
rendered as a dim-labelled line beneath week) renders a *fact line* (bar,
usage %, reset clock + relative duration) and a dimmed *forecast sub-line*
(projected % at reset and, when
the limit is crossed before reset, the clock at which usage hits 100%).
Shared quota semantics (forecasts, warnings, wording) live in
``claude_rotate.insights`` and are reused by the ``--report`` renderer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from claude_rotate.accounts import Account
from claude_rotate.config import (
    FORECAST_TAIL_MIN_SPAN_SECONDS,
    FORECAST_TAIL_WINDOW_5H_SECONDS,
    FORECAST_TAIL_WINDOW_7D_SECONDS,
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
from claude_rotate.providers import ProviderQuota, ProviderWindow
from claude_rotate.selection import ScopedLimit
from claude_rotate.sessions import SessionLoad

__all__ = [
    "DashboardRow",
    "attach_forecast_rates",
    "compact_one_liner",
    "fmt_sub_expiry",
    "forecast_enabled",
    "gradient_bar",
    "is_unusable",
    "render_dashboard",
    "render_providers",
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
    # Recent burn (%-points/sec) for the recency-aware forecast; None -> average pace.
    h5_rate_per_sec: float | None = None
    w7_rate_per_sec: float | None = None
    # Model-scoped weekly limits (e.g. Fable's separate weekly cap); display-only.
    w7_scoped: tuple[ScopedLimit, ...] = ()


class _RateSource(Protocol):
    def recent_rate(
        self,
        name: str,
        pct_now: float | None,
        *,
        window: str,
        now: float,
        tail_secs: int,
        min_span: int,
    ) -> float | None: ...


class _CacheSource(Protocol):
    def load(self, name: str) -> Any: ...


# Why a live probe failed -> what to tell the user when the cache is empty too.
# Anything not listed here (including an empty error) renders a bare no_data row.
NO_DATA_NOTES = {
    "rate_limited": "probe API rate-limited; no cached data",
    "upstream_error": "API 5xx — retry later",
    "timeout": "network error",
    "network_error": "network error",
}


def row_from_cache(
    candidate: Any, cache: _CacheSource, *, probe_error: str = ""
) -> tuple[Any | None, DashboardRow]:
    """Back-fill a failed probe from the usage cache.

    Returns ``(candidate, row)``. The candidate is the cache-filled copy when
    the cache had usable data and ``None`` when it did not — callers drop the
    ``None`` ones from the selection pool but still render the returned row, so
    an account never silently disappears from the dashboard.

    ``run`` and ``status`` classify probe failures slightly differently but
    recover identically; keeping the recovery here is what stops the two from
    drifting apart (they carried eight near-identical copies of it).
    """
    cached = cache.load(candidate.account.name)
    # An entry carrying no usage at all is not a fallback. ``update_scoped``
    # writes a minimal entry holding only ``w7_scoped``; that loads as ok with
    # both percentages None, and treating it as data would put the account back
    # in the selection pool as "usable" with entirely unknown quota.
    if cached is None or (cached.h5_pct is None and cached.w7_pct is None):
        note = NO_DATA_NOTES.get(probe_error.split(":")[0], "")
        return None, DashboardRow(
            account=candidate.account,
            h5_pct=None,
            w7_pct=None,
            h5_reset_secs=0,
            w7_reset_secs=0,
            status="no_data",
            note=note,
        )
    filled = replace(
        candidate,
        h5_pct=cached.h5_pct,
        w7_pct=cached.w7_pct,
        h5_reset_secs=cached.h5_reset_secs,
        w7_reset_secs=cached.w7_reset_secs,
        w7_opus_pct=cached.w7_opus_pct,
        w7_scoped=cached.w7_scoped,
    )
    return filled, DashboardRow(
        account=filled.account,
        h5_pct=filled.h5_pct,
        w7_pct=filled.w7_pct,
        h5_reset_secs=filled.h5_reset_secs,
        w7_reset_secs=filled.w7_reset_secs,
        from_cache=True,
        w7_scoped=filled.w7_scoped,
    )


def relogin_row(account: Account, note: str) -> DashboardRow:
    """A row for an account whose token needs user action."""
    return DashboardRow(
        account=account,
        h5_pct=None,
        w7_pct=None,
        h5_reset_secs=0,
        w7_reset_secs=0,
        status="relogin",
        note=note,
    )


def attach_forecast_rates(
    rows: list[DashboardRow], cache: _RateSource, now: float
) -> list[DashboardRow]:
    """Stamp each row's recent 5h/7d burn rate from the usage history (for the forecast).

    Looked up from the on-disk usage history; rows with no usable history keep ``None``
    and fall back to the average-pace projection. Call this once after building the rows
    and before rendering, so the dashboard, report and JSON all see the same rates.
    """
    out: list[DashboardRow] = []
    for r in rows:
        h5 = cache.recent_rate(
            r.account.name,
            r.h5_pct,
            window="5h",
            now=now,
            tail_secs=FORECAST_TAIL_WINDOW_5H_SECONDS,
            min_span=FORECAST_TAIL_MIN_SPAN_SECONDS,
        )
        w7 = cache.recent_rate(
            r.account.name,
            r.w7_pct,
            window="7d",
            now=now,
            tail_secs=FORECAST_TAIL_WINDOW_7D_SECONDS,
            min_span=FORECAST_TAIL_MIN_SPAN_SECONDS,
        )
        out.append(replace(r, h5_rate_per_sec=h5, w7_rate_per_sec=w7))
    return out


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


_WindowData = tuple[float | None, int, bool, int | None, float | None]


def _window_datas(
    rows: list[DashboardRow], window: str, *, now_local: datetime
) -> list[_WindowData]:
    """(pct, secs, from_cache, horizon, rate) per row for one window."""
    datas: list[_WindowData] = []
    for r in rows:
        pct = r.h5_pct if window == "5h" else r.w7_pct
        secs = r.h5_reset_secs if window == "5h" else r.w7_reset_secs
        rate = r.h5_rate_per_sec if window == "5h" else r.w7_rate_per_sec
        horizon_arg = expiry_horizon(r.account.effective_expires_at, secs, now_local)
        datas.append((pct if r.status == "ok" else None, secs, r.from_cache, horizon_arg, rate))
    return datas


def _cells_from_datas(
    datas: list[_WindowData],
    window_secs: int,
    *,
    now_local: datetime,
    show_forecast: bool,
) -> list[_WindowCell]:
    """Build cells from window datas; the weekday slot is shared across the batch."""

    def lands_on_other_day(secs: int) -> bool:
        return (now_local + timedelta(seconds=max(secs, 0))).date() != now_local.date()

    show_weekday = any(
        pct is not None
        and (
            lands_on_other_day(horizon_arg if horizon_arg is not None else secs)
            or (
                show_forecast
                and (eta := compute_limit_eta(pct, secs, window_secs, horizon_arg, rate))
                is not None
                and lands_on_other_day(eta)
            )
        )
        for pct, secs, _, horizon_arg, rate in datas
    )

    cells: list[_WindowCell] = []
    for pct, secs, from_cache, horizon_arg, rate in datas:
        if pct is None:
            cells.append(_NA_CELL)
            continue
        capped = horizon_arg is not None
        horizon = horizon_arg if horizon_arg is not None else secs
        forecast = (
            compute_forecast(pct, secs, window_secs, horizon_arg, rate) if show_forecast else None
        )
        eta = (
            compute_limit_eta(pct, secs, window_secs, horizon_arg, rate) if show_forecast else None
        )
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


def _window_cells(
    rows: list[DashboardRow],
    window: str,
    window_secs: int,
    *,
    now_local: datetime,
    show_forecast: bool,
) -> list[_WindowCell]:
    """Build one column of cells; the weekday slot is shared per column."""
    return _cells_from_datas(
        _window_datas(rows, window, now_local=now_local),
        window_secs,
        now_local=now_local,
        show_forecast=show_forecast,
    )


def _week_and_scoped_cells(
    rows: list[DashboardRow], *, now_local: datetime, show_forecast: bool
) -> tuple[list[_WindowCell], list[list[tuple[str, _WindowCell]]]]:
    """Week cells plus per-row ``(label, cell)`` scoped cells (e.g. Fable's weekly cap).

    Built in one batch so the week line and the scoped lines beneath it share
    the column's weekday slot and column grid. Scoped windows have no
    burn-rate history, so their forecast falls back to the average-pace
    projection (rate=None) — same as the ``--report`` renderer.
    """
    scoped_datas: list[_WindowData] = [
        (
            s.pct if r.status == "ok" else None,
            s.reset_secs,
            # A scoped value can be a cache backfill even when the row's
            # unified numbers are live — mark it stale (``~``) either way.
            r.from_cache or s.stale,
            expiry_horizon(r.account.effective_expires_at, s.reset_secs, now_local),
            None,
        )
        for r in rows
        for s in r.w7_scoped
    ]
    cells = _cells_from_datas(
        _window_datas(rows, "week", now_local=now_local) + scoped_datas,
        FORECAST_WINDOW_7D_SECONDS,
        now_local=now_local,
        show_forecast=show_forecast,
    )
    it = iter(cells[len(rows) :])
    scoped = [[(s.label, next(it)) for s in r.w7_scoped] for r in rows]
    return cells[: len(rows)], scoped


def _col_widths(cells: list[_WindowCell], *, include_rel: bool) -> tuple[int, int, int]:
    """(pct, clock, rel) column widths shared by fact line and sub-line."""
    pw = max((len(s) for c in cells for s in (c.pct_str, c.fc_str) if s), default=3)
    cw = max((len(s) for c in cells for s in (c.clock, c.eta_clock) if s), default=0)
    rw = 0
    if include_rel:
        rw = max((len(s) for c in cells for s in (c.rel, c.eta_rel) if s), default=0)
    return pw, cw, rw


def _window_text(
    c: _WindowCell,
    *,
    bar_w: int,
    pw: int,
    cw: int,
    rw: int,
    label: str = "",
    label_style: str = "",
) -> Text:
    """Fact line + optional forecast sub-line for one window cell."""
    t = Text()
    if label:
        t.append(label, style=label_style)
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


def _scoped_label_width(scoped: list[list[tuple[str, _WindowCell]]]) -> int:
    """Width of the dim label prefix (longest scoped label + a space); 0 when none."""
    return max((len(lbl) + 1 for per_row in scoped for lbl, _ in per_row), default=0)


def _append_scoped_lines(
    t: Text,
    scoped_row: list[tuple[str, _WindowCell]],
    *,
    bar_w: int,
    pw: int,
    cw: int,
    rw: int,
    label_w: int,
) -> None:
    """Full fact + forecast lines per scoped limit, beneath the week lines."""
    for lbl, sc in scoped_row:
        if sc.pct is None:
            continue
        t.append("\n")
        t.append_text(
            _window_text(
                sc,
                bar_w=bar_w,
                pw=pw,
                cw=cw,
                rw=rw,
                label=lbl.ljust(label_w),
                label_style="dim",
            )
        )


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
    cells7, scoped7 = _week_and_scoped_cells(rows, now_local=now_local, show_forecast=show_forecast)
    flat_scoped = [c for per_row in scoped7 for _, c in per_row]
    label7_w = _scoped_label_width(scoped7)
    labels = [_label_text(r, chosen=chosen, active=active) for r in rows]
    subs = [_sub_text(r, now=now) for r in rows]
    label_w = max((max(len(ln) for ln in lbl.plain.split("\n")) for lbl in labels), default=0)
    sub_w = max((max(len(ln) for ln in s.plain.split("\n")) for s in subs if s.plain), default=0)
    sub_w = max(sub_w, len("sub"))

    for include_rel in (True, False):
        pw5, cw5, rw5 = _col_widths(cells5, include_rel=include_rel)
        pw7, cw7, rw7 = _col_widths(cells7 + flat_scoped, include_rel=include_rel)

        def text_w(pw: int, cw: int, rw: int) -> int:
            return 2 + pw + ((2 + cw) if cw else 0) + ((1 + rw) if rw else 0)

        # Bordered-table chrome: 5 vertical rules + 4 columns x 2 padding cells.
        chrome = 5 + 4 * 2
        overhead = (
            label_w + sub_w + chrome + text_w(pw5, cw5, rw5) + text_w(pw7, cw7, rw7) + label7_w
        )
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
        for row, lbl, c5, c7, sc7, sub in zip(
            rows, labels, cells5, cells7, scoped7, subs, strict=True
        ):
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
            # Blank prefix keeps the week bar aligned with labelled scoped bars.
            t7 = _window_text(c7, bar_w=bar_w, pw=pw7, cw=cw7, rw=rw7, label=" " * label7_w)
            if c7.pct is not None:
                _append_scoped_lines(t7, sc7, bar_w=bar_w, pw=pw7, cw=cw7, rw=rw7, label_w=label7_w)
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
    scoped: list[tuple[str, _WindowCell]],
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

    windows: list[tuple[str, _WindowCell]] = [("5h", c5), ("week", c7)]
    if c7.pct is not None:
        windows += [(lbl, sc) for lbl, sc in scoped if sc.pct is not None]
    cells = [c for _, c in windows]
    pw = max((len(s) for c in cells for s in (c.pct_str, c.fc_str) if s), default=3)
    cw = max((len(s) for c in cells for s in (c.clock, c.eta_clock) if s), default=0)
    rw = max((len(s) for c in cells for s in (c.rel, c.eta_rel) if s), default=0)
    label_w = max(len(lbl) for lbl, _ in windows) + 2
    for label, cell in windows:
        line = _window_text(
            cell,
            bar_w=_CARD_BAR_WIDTH,
            pw=pw,
            cw=cw,
            rw=rw,
            label=label.ljust(label_w),
            label_style="dim" if label not in ("5h", "week") else "",
        )
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
    cells7, scoped7 = _week_and_scoped_cells(rows, now_local=now_local, show_forecast=show_forecast)
    table = Table(
        box=box.ROUNDED,
        show_lines=True,  # rule between accounts — each card reads as its own band
        show_header=False,
        padding=(0, 1),
        border_style="dim",
    )
    table.add_column("", no_wrap=True)
    for row, c5, c7, sc7 in zip(rows, cells5, cells7, scoped7, strict=True):
        unusable = is_unusable(row, now=now)
        table.add_row(
            _card_text(row, c5, c7, sc7, chosen=chosen, active=active, now=now, unusable=unusable)
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
    rows: list[DashboardRow],
    *,
    chosen: str | None,
    active: str | None = None,
    now: datetime | None = None,
    providers: list[ProviderQuota] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    return {
        "chosen": chosen,
        "active": active,
        # Always present, so consumers can rely on the key rather than probing.
        "providers": [
            {
                "provider": q.provider,
                "account": q.account,
                "note": q.note,
                "windows": [
                    {"label": w.label, "used_pct": w.used_pct, "reset_secs": w.reset_secs}
                    for w in q.windows
                ],
            }
            for q in (providers or [])
        ],
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
                    r.h5_pct,
                    r.h5_reset_secs,
                    FORECAST_WINDOW_5H_SECONDS,
                    expiry_horizon(r.account.effective_expires_at, r.h5_reset_secs, now),
                    r.h5_rate_per_sec,
                ),
                "w7_forecast_pct": compute_forecast(
                    r.w7_pct,
                    r.w7_reset_secs,
                    FORECAST_WINDOW_7D_SECONDS,
                    expiry_horizon(r.account.effective_expires_at, r.w7_reset_secs, now),
                    r.w7_rate_per_sec,
                ),
                "w7_scoped": [
                    {
                        "label": s.label,
                        "pct": s.pct,
                        "reset_secs": s.reset_secs,
                        "stale": s.stale,
                        "age_secs": s.age_secs,
                    }
                    for s in r.w7_scoped
                ],
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


_PROVIDER_WINDOWS = ("5h", "week")
_PROVIDER_BAR_MAX = 12
_PROVIDER_BAR_MIN = 6
# Width of the usage column, shared by the layout maths and the cell that
# renders it — they drift apart silently otherwise, and Rich pays for it
# by truncating the reset clock.
_PROVIDER_PCT_W = 7


def render_providers(
    quotas: list[ProviderQuota],
    *,
    console: Console,
    now: datetime | None = None,
) -> None:
    """The other subscriptions on this machine, as a table of their own.

    Deliberately thinner than the Anthropic dashboard above it: bar, usage
    and reset, nothing else. These providers have no rotation, no forecast
    history and no subscription expiry to show, and a column for them would
    stand empty forever.

    Widths adapt like ``_render_table`` does — relative durations are dropped
    before the bar shrinks, because a truncated reset clock is worse than no
    ``(4h 55m)`` at all.
    """
    if not quotas:
        return
    now = now or datetime.now(UTC)
    now_local = now.astimezone()

    labels = [_provider_label(q) for q in quotas]
    label_w = max(max(len(ln) for ln in lbl.plain.split("\n")) for lbl in labels)
    bar_w, include_rel = _provider_layout(quotas, console.width, label_w, now_local)

    table = Table(box=box.ROUNDED, padding=(0, 1), border_style="dim", header_style="bold")
    table.add_column("", no_wrap=True)
    for header in _PROVIDER_WINDOWS:
        table.add_column(header, no_wrap=True)

    for quota, label in zip(quotas, labels, strict=True):
        by_label = {w.label: w for w in quota.windows}
        cells = [
            _provider_cell(by_label.get(name), now_local, bar_w, include_rel)
            for name in _PROVIDER_WINDOWS
        ]
        if not quota.windows and quota.note:
            # Nothing to plot — let the reason take the row instead of two blanks.
            cells = [Text(quota.note, style="yellow"), Text("")]
        table.add_row(label, *cells)

    console.print()
    console.print(Text("  other providers", style="dim"))
    console.print(table)


def _provider_layout(
    quotas: list[ProviderQuota], width: int, label_w: int, now_local: datetime
) -> tuple[int, bool]:
    """Widest bar that still fits, and whether relative durations survive."""
    clock_w = max(
        (
            len(clock_at(now_local, w.reset_secs, show_weekday=True))
            for q in quotas
            for w in q.windows
        ),
        default=0,
    )
    rel_w = max(
        (len(rel_duration(w.reset_secs)) for q in quotas for w in q.windows),
        default=0,
    )
    # Bordered-table chrome: 4 vertical rules + 3 columns x 2 padding cells.
    chrome = 4 + 3 * 2
    for include_rel in (True, False):
        text_w = _PROVIDER_PCT_W + (2 + clock_w) + ((1 + rel_w) if include_rel else 0)
        slack = (width - label_w - chrome - 2 * text_w) // 2
        if slack >= _PROVIDER_BAR_MIN:
            return min(slack, _PROVIDER_BAR_MAX), include_rel
    return _PROVIDER_BAR_MIN, False


def _provider_label(quota: ProviderQuota) -> Text:
    label = Text(quota.provider, style="bold")
    if quota.account and quota.account != quota.provider:
        label.append(f"\n{quota.account}", style="dim")
    if quota.note and quota.windows:
        label.append(f"\n{quota.note}", style="dim italic")
    return label


def _provider_cell(
    window: ProviderWindow | None, now_local: datetime, bar_w: int, include_rel: bool
) -> Text:
    if window is None:
        return Text("\u2014", style="grey50")
    cell = gradient_bar(window.used_pct, width=bar_w)
    cell.append(f"{window.used_pct:>{_PROVIDER_PCT_W - 1}.0f}%")
    if window.reset_secs:
        reset = f"  {clock_at(now_local, window.reset_secs, show_weekday=True)}"
        if include_rel:
            reset += f" {rel_duration(window.reset_secs)}"
        cell.append(reset, style="dim")
    return cell
