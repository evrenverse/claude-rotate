"""Tests for the `status --report` renderer (claude_rotate.report)."""

from __future__ import annotations

from datetime import UTC, datetime

from claude_rotate.accounts import Account
from claude_rotate.dashboard import DashboardRow
from claude_rotate.report import build_report

# 2026-06-05 00:40 UTC is a Friday — a stable reference for clock/weekday math.
NOW = datetime(2026, 6, 5, 0, 40, tzinfo=UTC)

_MIN = 60
_HOUR = 3600
_DAY = 86400


def _account(name: str, *, expires: datetime | None = None, pinned: bool = False) -> Account:
    return Account(
        name=name,
        runtime_token="tok",
        label=name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        plan="max_20x",
        email=f"{name}@example.com",
        subscription_expires_at=expires,
        pinned=pinned,
    )


def _row(
    name: str,
    *,
    h5: float | None,
    w7: float | None,
    h5_reset: int = 0,
    w7_reset: int = 0,
    expires: datetime | None = None,
    status: str = "ok",
    note: str = "",
) -> DashboardRow:
    return DashboardRow(
        account=_account(name, expires=expires),
        h5_pct=h5,
        w7_pct=w7,
        h5_reset_secs=h5_reset,
        w7_reset_secs=w7_reset,
        status=status,
        note=note,
    )


def _sample() -> list[DashboardRow]:
    return [
        _row("grace", h5=44.0, w7=94.0, h5_reset=19 * _MIN, w7_reset=2 * _DAY + 8 * _HOUR),
        _row(
            "matri",
            h5=12.0,
            w7=100.0,
            h5_reset=3 * _HOUR + 53 * _MIN,
            w7_reset=3 * _DAY + 2 * _HOUR,
        ),
        _row(
            "stamp", h5=0.0, w7=0.0, h5_reset=3 * _HOUR + 53 * _MIN, w7_reset=6 * _DAY + 12 * _HOUR
        ),
    ]


def _fence_lines(report: str) -> list[str]:
    """All lines inside the report's Markdown code fences (one fence per account)."""
    out: list[str] = []
    inside = False
    for ln in report.splitlines():
        if ln == "```":
            inside = not inside
            continue
        if inside:
            out.append(ln)
    return out


def _header_lines(report: str) -> list[str]:
    """Account header lines inside the card region (not metric or forecast lines)."""
    return [
        ln
        for ln in _fence_lines(report)
        if ln.strip()
        and not ln.lstrip().startswith(("5h", "week"))
        and not ln.lstrip().startswith(("→", "—", "reached"))
    ]


def _blocks(report: str) -> list[list[str]]:
    """The per-account blocks (the lines of each individual code fence)."""
    blocks: list[list[str]] = []
    cur: list[str] = []
    inside = False
    for ln in report.splitlines():
        if ln == "```":
            if inside:
                blocks.append(cur)
                cur = []
            inside = not inside
            continue
        if inside:
            cur.append(ln)
    return blocks


def test_marker_both_when_active_equals_chosen() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    assert "@> grace" in report
    # the other rows carry neither marker
    assert "   matri" in report
    assert "   stamp" in report


def test_markers_split_when_active_and_chosen_differ() -> None:
    report = build_report(_sample(), chosen="stamp", active="grace", now=NOW)
    assert "@  grace" in report  # active only
    assert " > stamp" in report  # next pick only
    # the active account's header must NOT also carry the chosen marker
    grace_hdr = next(ln for ln in _header_lines(report) if "grace" in ln)
    assert ">" not in grace_hdr


def test_active_row_sorted_first() -> None:
    report = build_report(_sample(), chosen="stamp", active="stamp", now=NOW)
    assert "stamp" in _header_lines(report)[0]


def test_card_values_aligned_within_each_block() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # Alignment is per section: within each block the current-% column (the
    # first '%' on each metric line) stacks vertically.
    for block in _blocks(report):
        metric_lines = [ln for ln in block if ln.lstrip().startswith(("5h", "week"))]
        pct_columns = {ln.index("%") for ln in metric_lines}
        assert len(pct_columns) == 1, f"percent column misaligned in {block}: {pct_columns}"


def test_one_code_block_per_account() -> None:
    rows = _sample()
    report = build_report(rows, chosen="grace", active="grace", now=NOW)
    fences = [ln for ln in report.splitlines() if ln == "```"]
    assert len(fences) == 2 * len(rows)  # one open + one close fence per account


def test_progress_bar_full_and_empty() -> None:
    report = build_report(_sample(), chosen="stamp", active="stamp", now=NOW)
    lines = _fence_lines(report)
    assert "█" in report and "░" in report  # bars are rendered at all
    zero_line = next(ln for ln in lines if "  0%" in ln)
    assert "█" not in zero_line and "░" in zero_line  # 0% → empty bar
    full_line = next(ln for ln in lines if "100%" in ln)
    assert "░" not in full_line and "█" in full_line  # 100% → full bar


def test_reset_shows_clock_and_relative() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # absolute clock followed by a compact relative duration in parentheses
    assert "00:59" in report  # grace 5h reset clock (today)
    assert "(19m)" in report  # grace 5h reset, relative
    assert "(3h 53m)" in report  # matri/stamp 5h reset, relative
    assert "(2d 8h)" in report  # grace weekly reset, relative


def test_reset_shows_weekday_for_dated_reset() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # weekly resets land on other days → weekday prefix on the clock
    assert "Sun 08:40" in report  # grace weekly reset
    assert "Mon 02:40" in report  # matri weekly reset
    assert "Thu 12:40" in report  # stamp weekly reset


def _grace_block(report: str) -> list[str]:
    return next(b for b in _blocks(report) if "grace" in b[0])


def _subline_after(block: list[str], label: str) -> str:
    """The forecast sub-line that follows a given window's fact line."""
    fact = next(ln for ln in block if ln.lstrip().startswith(label))
    return block[block.index(fact) + 1]


def test_forecast_on_its_own_arrow_prefixed_subline() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # grace weekly 94% projects to 141% by reset → now on its own ``→``-prefixed
    # sub-line beneath the week fact line, no longer a fact-line column.
    sub = _subline_after(_grace_block(report), "week")
    assert "→141%" in sub
    assert not sub.lstrip().startswith(("5h", "week"))  # the sub-line is label-less


def test_limit_eta_shown_when_forecast_reaches_limit() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # grace weekly 94% with 403200s elapsed crosses 100% in 25736s ≈ 7h 8m, i.e.
    # at 07:48 counting from 00:40 → absolute clock + relative on the sub-line.
    sub = _subline_after(_grace_block(report), "week")
    assert "07:48" in sub  # absolute limit-hit clock
    assert "(7h 8m)" in sub  # relative limit-hit duration


def test_no_eta_when_forecast_stays_under_limit() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # grace 5h 44% only projects to 46% (< 100) → forecast shown, ETA collapses
    # to a trailing em-dash (the window resets before the wall).
    sub = _subline_after(_grace_block(report), "5h")
    assert "→46%" in sub
    assert sub.rstrip().endswith("—")


def test_reached_subline_when_already_at_limit() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # matri weekly is at 100% → its sub-line is the lone word 'reached'.
    assert any(ln.strip() == "reached" for ln in _fence_lines(report))


def test_no_trend_subline_is_lone_dash() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # stamp 5h/week are 0% → no trend → the sub-line is a lone em-dash.
    assert any(ln.strip() == "—" for ln in _fence_lines(report))


def test_forecast_subline_columns_align_under_facts() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # The forecast %-column stacks under the current %-column, and the whole
    # sub-line shares the fact line's grid (so the relative duration ends in the
    # same column too).
    block = _grace_block(report)
    week_fact = next(ln for ln in block if ln.lstrip().startswith("week"))
    week_sub = block[block.index(week_fact) + 1]
    assert "→141%" in week_sub
    assert week_fact.index("%") == week_sub.index("%")
    assert week_fact.rindex(")") == week_sub.rindex(")")


def test_report_drops_quota_risk_and_fallback() -> None:
    # Heavy weekly usage but ok status and no near expiry → no warnings block.
    rows = [
        _row("a", h5=95.0, w7=95.0, h5_reset=_HOUR, w7_reset=_DAY),
        _row("b", h5=5.0, w7=10.0, h5_reset=_HOUR, w7_reset=_DAY),
    ]
    report = build_report(rows, chosen="a", active="a", now=NOW)
    assert "at risk" not in report
    assert "Fallback" not in report
    assert "⚠️ Warnings:" not in report


def test_error_row_surfaces_status_not_numbers() -> None:
    rows = [
        _row("a", h5=10.0, w7=20.0, h5_reset=_HOUR, w7_reset=_DAY),
        _row("dead", h5=None, w7=None, status="relogin", note="token invalid"),
    ]
    report = build_report(rows, chosen="a", active="a", now=NOW)
    assert "dead: relogin — token invalid." in report
    assert "N/A" in report  # dead row's percentages


def test_expiry_warns_under_seven_days() -> None:
    soon = datetime(2026, 6, 8, 0, 40, tzinfo=UTC)  # 3 days out
    rows = [_row("a", h5=10.0, w7=20.0, h5_reset=_HOUR, w7_reset=_DAY, expires=soon)]
    report = build_report(rows, chosen="a", active="a", now=NOW)
    assert "subscription expires in 3d." in report


def test_fenced_toggle() -> None:
    rows = _sample()
    assert "```" in build_report(rows, chosen="grace", active="grace", now=NOW, fenced=True)
    assert "```" not in build_report(rows, chosen="grace", active="grace", now=NOW, fenced=False)


def test_status_line_no_active_session() -> None:
    report = build_report(_sample(), chosen="grace", active=None, now=NOW)
    assert "No active session recorded; next launch would pick 'grace' (>)." in report
