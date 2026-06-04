"""Tests for the `status --report` renderer (claude_rotate.report)."""

from __future__ import annotations

from datetime import UTC, datetime

from claude_rotate.accounts import Account
from claude_rotate.dashboard import DashboardRow
from claude_rotate.report import build_report, format_reset_column

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


def _box_lines(report: str) -> list[str]:
    return [ln for ln in report.splitlines() if ln and ln[0] in "┌│├└"]


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
    # the active table row must NOT also carry the chosen marker
    grace_row = next(ln for ln in _box_lines(report) if "grace" in ln)
    assert ">" not in grace_row


def test_active_row_sorted_first() -> None:
    report = build_report(_sample(), chosen="stamp", active="stamp", now=NOW)
    data = [ln for ln in _box_lines(report) if ln.startswith("│") and "Account" not in ln]
    assert "stamp" in data[0]


def test_all_box_lines_share_one_width() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    widths = {len(ln) for ln in _box_lines(report)}
    assert len(widths) == 1, f"box lines misaligned: {widths}"


def test_reset_place_values_align_5h() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # 5h resets all land today → no weekday; hours field always shown.
    assert "00:59 (0h 19m)" in report
    assert "04:33 (3h 53m)" in report


def test_reset_shows_weekday_and_padded_hours_weekly() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    # weekly resets land on other days → weekday prefix; hours padded to width 2.
    assert "Sun 08:40 (2d  8h)" in report
    assert "Mon 02:40 (3d  2h)" in report
    assert "Thu 12:40 (6d 12h)" in report


def test_format_reset_column_pads_to_common_width() -> None:
    cells = format_reset_column([8 * _HOUR, 12 * _HOUR + 0], now=NOW)
    # both sub-day → "Xh Ym"; 8 vs 12 → hours padded so units line up
    assert cells[0].endswith("( 8h 0m)")
    assert cells[1].endswith("(12h 0m)")
    assert len({len(c) for c in cells}) == 1


def test_format_reset_column_none_renders_dash() -> None:
    assert format_reset_column([None, None], now=NOW) == ["-", "-"]


def test_warnings_flag_weekly_risk_and_forecast() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    assert "⚠️ Warnings:" in report
    assert "grace (active): week 94%, forecast 141% → weekly limit at risk." in report
    assert "matri: week 100% → weekly limit at risk." in report


def test_warnings_fallback_is_freest_non_active() -> None:
    report = build_report(_sample(), chosen="grace", active="grace", now=NOW)
    assert "Fallback: stamp (week 0%, forecast 0%)." in report


def test_healthy_when_no_risks() -> None:
    rows = [
        _row("a", h5=10.0, w7=20.0, h5_reset=_HOUR, w7_reset=_DAY),
        _row("b", h5=5.0, w7=10.0, h5_reset=_HOUR, w7_reset=_DAY),
    ]
    report = build_report(rows, chosen="a", active="a", now=NOW)
    assert "✅ All accounts healthy." in report


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
