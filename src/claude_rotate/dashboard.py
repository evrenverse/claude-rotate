"""Dashboard rendering with rich.

This task introduces the gradient-bar primitive. Tasks 24+25 layer the table,
footers, and non-TTY mode on top.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from claude_rotate.accounts import Account
from claude_rotate.config import (
    FORECAST_WINDOW_5H_SECONDS,
    FORECAST_WINDOW_7D_SECONDS,
    STALE_METADATA_WARN_DAYS,
)

_FILLED = "█"
_EMPTY = "░"


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


# ---------------------------------------------------------------------------
# Task 24: DashboardRow dataclass, formatters, and render_dashboard
# ---------------------------------------------------------------------------


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


# Fixed column widths so reset strings line up vertically between rows.
# ``_fmt_5h`` is always 6 chars (``5h 59m``); ``_fmt_weekly`` is always
# 10 chars (``7d 23h 59m``) — we always include the day counter, even at
# 0d, so both cells below the "weekly" header start at the same column.
_RESET_WIDTH_5H = 6
_RESET_WIDTH_WEEKLY = 10

# Fixed slot for the forecast bracket so the reset column stays aligned across
# rows. Widest token is " [→999%]" (leading space + 7 chars) = 8.
_FORECAST_WIDTH = 8


def _fmt_duration(secs: int, *, include_days: bool) -> str:
    """Format ``secs`` as a duration, dropping any zero-valued component.

    ``2d 0h 45m`` → ``2d 45m``, ``5h 0m`` → ``5h``, ``0h 23m`` → ``23m``.
    Paired with right-aligned padding in ``_bar_pct_reset`` so unit
    markers (``d``/``h``/``m``) still line up vertically across rows
    despite the variable-length output.
    """
    if secs <= 0:
        return ""
    if include_days:
        d, rem = divmod(secs, 86400)
    else:
        d, rem = 0, secs
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d > 0:
        parts.append(f"{d}d")
    if h > 0:
        parts.append(f"{h}h")
    if m > 0:
        parts.append(f"{m}m")
    return " ".join(parts) if parts else "<1m"


def _fmt_5h(secs: int) -> str:
    return _fmt_duration(secs, include_days=False)


def _fmt_weekly(secs: int) -> str:
    return _fmt_duration(secs, include_days=True)


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
    """Return (text, rich-style) for the ``expires`` column.

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


def forecast_enabled() -> bool:
    """Whether the status dashboard renders the [→XX%] forecast.

    On by default; ``CLAUDE_ROTATE_FORECAST=0`` disables it. Mirrors the toggle
    in the separate (external) Bash statusline project so the two UIs agree.
    """
    return os.environ.get("CLAUDE_ROTATE_FORECAST", "1") != "0"


def _bar_pct_reset(
    pct: float | None,
    reset_secs: int,
    fmt_reset: Any,
    from_cache: bool,
    *,
    width: int = 12,
    reset_width: int = 0,
    forecast: int | None = None,
    forecast_slot: int = 0,
) -> Text:
    """Combine gradient bar + coloured pct + fixed-width reset into one Text cell.

    ``reset_width`` pads (or reserves) the reset-time slot so the value
    sits at the same column regardless of whether this row had usage in
    the window or not. Rows with empty reset (usage <1 min or 0%) still
    get the trailing whitespace so the next column stays aligned.
    """
    cell = Text()
    if pct is None:
        cell.append("N/A", style="grey50")
        if reset_width:
            # pad the rest so the following column aligns (incl. forecast slot)
            cell.append(" " * (width + 5 + forecast_slot + 2 + reset_width - len("N/A")))
        return cell

    # Gradient bar
    bar = gradient_bar(pct, width=width)
    cell.append_text(bar)

    # Single-space separator, then pct in gradient colour. Right-justify
    # within 3 digits so 0/26/100 share a vertical anchor.
    colour = _pct_color(pct, width=width)
    prefix = "~" if from_cache else ""
    pct_str = f" {prefix}{pct:>3g}%"
    cell.append(pct_str, style=colour)

    if forecast_slot:
        if forecast is not None:
            token = f"[→{forecast}%]"
            cell.append(" ")
            cell.append(token, style=_pct_color(float(forecast), width=width))
            pad = forecast_slot - 1 - len(token)
            if pad > 0:
                cell.append(" " * pad)
        else:
            cell.append(" " * forecast_slot)

    reset_str = fmt_reset(reset_secs) or ""
    if reset_width:
        # Right-justify so the trailing "m" / "h" / "d" unit marker lines
        # up vertically across rows, regardless of whether the shorter row
        # dropped its leading ``0d``/``0h`` segment.
        cell.append(f"  {reset_str:>{reset_width}}")
    elif reset_str:
        cell.append(f"  {reset_str}")

    return cell


def render_dashboard(
    rows: list[DashboardRow],
    *,
    chosen: str | None,
    console: Console,
    now: datetime | None = None,
    show_forecast: bool = True,
) -> None:
    now = now or datetime.now(UTC)

    # One table — header is row 0, so column widths (content-driven) are
    # shared between header and body. Separate tables compute widths
    # independently which misaligns the column labels.
    # `no_wrap` on the bar columns prevents rich from wrapping "  3h 52m"
    # onto its own line when the terminal is narrow.
    # Merge the chosen-marker into the label cell so ``>`` sits flush
    # against the label (with one space) instead of separated by the
    # column padding.
    # Fixed min_width keeps headers aligned even when every row is in an
    # error state like ``N/A`` — without them, Rich shrinks the columns
    # to their narrowest content and the "5h" / "weekly" headers drift
    # left into the label column.
    # Widths are: gradient_bar(12) + " " + pct(4) + "  " + reset(6 or 10).
    fc_extra = _FORECAST_WIDTH if show_forecast else 0
    _H5_WIDTH = 12 + 1 + 4 + fc_extra + 2 + _RESET_WIDTH_5H
    _W7_WIDTH = 12 + 1 + 4 + fc_extra + 2 + _RESET_WIDTH_WEEKLY
    table = Table.grid(padding=(0, 2))
    table.add_column("label", no_wrap=True)
    table.add_column("h5_combined", no_wrap=True, min_width=_H5_WIDTH)
    table.add_column("w7_combined", no_wrap=True, min_width=_W7_WIDTH)
    # Right-justify so the trailing "d" lines up across "2d" / "25d".
    table.add_column("expires", no_wrap=True, justify="right")

    def _label(row: DashboardRow) -> str:
        # Marker priority: pinned wins over chosen, because a pinned
        # account is always chosen — the ★ carries more information.
        if row.account.pinned:
            marker = "[yellow]★[/]"
        elif chosen == row.account.name:
            marker = "[green]>[/]"
        else:
            marker = " "
        return f"{marker} {row.account.label}"

    # Header as first row (blank label cell preserves column alignment)
    table.add_row("", "5h", "weekly", "expires")

    for row in rows:
        if row.status == "relogin":
            table.add_row(
                _label(row),
                "N/A",
                f"[red]RELOGIN[/]  [dim]{row.note}[/]",
                "",
            )
            continue
        if row.status == "rate_limited":
            table.add_row(
                _label(row),
                "N/A",
                f"[yellow]LIMITED[/]  [dim]{row.note}[/]",
                "",
            )
            continue
        if row.status == "sub_canceled":
            table.add_row(
                _label(row),
                "N/A",
                f"[red]CANCELED[/]  [dim]{row.note}[/]",
                "",
            )
            continue

        h5_cell = _bar_pct_reset(
            row.h5_pct,
            row.h5_reset_secs,
            _fmt_5h,
            row.from_cache,
            reset_width=_RESET_WIDTH_5H,
            forecast=(
                compute_forecast(row.h5_pct, row.h5_reset_secs, FORECAST_WINDOW_5H_SECONDS)
                if show_forecast
                else None
            ),
            forecast_slot=fc_extra,
        )
        w7_cell = _bar_pct_reset(
            row.w7_pct,
            row.w7_reset_secs,
            _fmt_weekly,
            row.from_cache,
            reset_width=_RESET_WIDTH_WEEKLY,
            forecast=(
                compute_forecast(row.w7_pct, row.w7_reset_secs, FORECAST_WINDOW_7D_SECONDS)
                if show_forecast
                else None
            ),
            forecast_slot=fc_extra,
        )
        exp_text, exp_style = fmt_sub_expiry(
            row.account.effective_expires_at,
            status=row.account.subscription_status,
            now=now,
        )
        exp_cell = f"[{exp_style}]{exp_text}[/]" if exp_style else exp_text
        table.add_row(_label(row), h5_cell, w7_cell, exp_cell)

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# Task 25: stale-metadata footer, compact non-TTY one-liner, status JSON
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


def status_json(rows: list[DashboardRow], *, chosen: str | None) -> dict[str, Any]:
    return {
        "chosen": chosen,
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
                "subscription_expires_at": (
                    r.account.effective_expires_at.isoformat()
                    if r.account.effective_expires_at
                    else None
                ),
            }
            for r in rows
        ],
    }
