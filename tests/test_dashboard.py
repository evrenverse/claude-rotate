from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO

from rich.console import Console
from rich.text import Text

from claude_rotate.accounts import Account
from claude_rotate.dashboard import (
    DashboardRow,
    compute_forecast,
    compute_limit_eta,
    gradient_bar,
    render_dashboard,
)


def test_gradient_bar_produces_text_instance() -> None:
    bar = gradient_bar(50.0, width=10)
    assert isinstance(bar, Text)


def test_gradient_bar_length_matches_width() -> None:
    bar = gradient_bar(50.0, width=12)
    # rich.Text.cell_len is the count of terminal cells (1 per box char here)
    assert bar.cell_len == 12


def test_gradient_bar_zero_pct_has_no_filled_cells() -> None:
    # Render with NO_COLOR so we get a plain ASCII-ish capture
    console = Console(force_terminal=False, no_color=True, width=40)
    with console.capture() as cap:
        console.print(gradient_bar(0.0, width=10), end="")
    s = cap.get()
    # Expect zero filled blocks
    assert "█" not in s


def test_gradient_bar_full_pct_fills_all_cells() -> None:
    console = Console(force_terminal=False, no_color=True, width=40)
    with console.capture() as cap:
        console.print(gradient_bar(100.0, width=10), end="")
    s = cap.get()
    assert s.count("█") == 10


def test_gradient_bar_clamps_above_100() -> None:
    console = Console(force_terminal=False, no_color=True, width=40)
    with console.capture() as cap:
        console.print(gradient_bar(250.0, width=8), end="")
    assert cap.get().count("█") == 8


def _acc(name: str, plan: str = "max_20x", sub_days: int | None = None) -> Account:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    return Account(
        name=name,
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label=name.title(),
        created_at=now,
        plan=plan,
        subscription_expires_at=(now + timedelta(days=sub_days)) if sub_days else None,
    )


def _row(
    account: Account,
    h5_pct: float | None = 10.0,
    w7_pct: float | None = 20.0,
    h5_secs: int = 3600,
    w7_secs: int = 86400,
    from_cache: bool = False,
    status: str = "ok",
    note: str = "",
) -> DashboardRow:
    return DashboardRow(
        account=account,
        h5_pct=h5_pct,
        w7_pct=w7_pct,
        h5_reset_secs=h5_secs,
        w7_reset_secs=w7_secs,
        from_cache=from_cache,
        status=status,
        note=note,
    )


def test_render_contains_name_and_percentages() -> None:
    rows = [_row(_acc("main"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen=rows[0].account.name, console=console)
    out = console.file.getvalue()
    assert "main" in out
    assert "10" in out  # h5 pct
    assert "20" in out  # w7 pct


def test_render_marks_chosen_row_with_arrow() -> None:
    rows = [_row(_acc("a")), _row(_acc("b"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen="b", console=console)
    out = console.file.getvalue()
    # The '>' marker is only on the chosen row
    assert ">" in out


def test_render_cache_rows_get_tilde_prefix() -> None:
    rows = [_row(_acc("main"), from_cache=True)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen="main", console=console)
    assert "~" in console.file.getvalue()


def test_render_relogin_row_shows_relogin_label() -> None:
    rows = [
        _row(_acc("main"), h5_pct=None, w7_pct=None, status="relogin", note="token expired (401)")
    ]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen=None, console=console)
    out = console.file.getvalue()
    assert "RELOGIN" in out.upper()
    assert "401" in out


def test_render_rate_limited_row_shows_limited_label() -> None:
    rows = [
        _row(
            _acc("grace2"),
            h5_pct=None,
            w7_pct=None,
            status="rate_limited",
            note="quota exhausted (429)",
        )
    ]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen=None, console=console)
    out = console.file.getvalue()
    assert "LIMITED" in out
    assert "RATE-LIMITED" not in out


def test_render_sub_canceled_row_shows_canceled_label() -> None:
    rows = [
        _row(
            _acc("work"),
            h5_pct=None,
            w7_pct=None,
            status="sub_canceled",
            note="subscription ended",
        )
    ]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen=None, console=console)
    out = console.file.getvalue()
    assert "CANCELED" in out
    assert "SUB-CANCELED" not in out


def test_render_rate_limited_note_on_same_line() -> None:
    """The note 'quota exhausted (429)' must appear on the same line as the label."""
    rows = [
        _row(
            _acc("grace2"),
            h5_pct=None,
            w7_pct=None,
            status="rate_limited",
            note="quota exhausted (429)",
        )
    ]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen=None, console=console)
    out = console.file.getvalue()
    lines = out.split("\n")
    assert any("LIMITED" in line and "quota exhausted (429)" in line for line in lines)


def test_render_shows_subscription_expiry_in_days() -> None:
    fixed_now = datetime(2026, 4, 22, tzinfo=UTC)
    rows = [_row(_acc("main", sub_days=5))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen="main", console=console, now=fixed_now)
    out = console.file.getvalue()
    assert "5d" in out or "5 d" in out


def test_compact_one_liner_format() -> None:
    from claude_rotate.dashboard import compact_one_liner

    a = _acc("main")
    row = _row(a, h5_pct=6.0, w7_pct=85.0)
    s = compact_one_liner(row)
    assert "main" in s.lower() or "Main" in s
    assert "Max-20" in s or "max_20x" in s
    assert "6%" in s
    assert "85%" in s


def test_status_json_includes_all_relevant_fields() -> None:
    from claude_rotate.dashboard import status_json

    rows = [_row(_acc("main"), h5_pct=6.0, w7_pct=85.0)]
    payload = status_json(rows, chosen="main")
    assert payload["chosen"] == "main"
    assert payload["accounts"][0]["name"] == "main"
    assert payload["accounts"][0]["h5_pct"] == 6.0
    assert payload["accounts"][0]["w7_pct"] == 85.0
    # token_expires_at is removed in v6 — confirm it is absent
    assert "token_expires_at" not in payload["accounts"][0]


def test_status_json_no_token_expires_at_field() -> None:
    """status_json must NOT include the removed token_expires_at field."""
    from claude_rotate.dashboard import status_json

    rows = [_row(_acc("main"))]
    payload = status_json(rows, chosen="main")
    assert "token_expires_at" not in payload["accounts"][0]


# ---------------------------------------------------------------------------
# Dashboard polish: pct next to bar, header alignment
# ---------------------------------------------------------------------------


def test_render_header_contains_5h_and_week() -> None:
    """The header row must mention '5h' and 'week'."""
    rows = [_row(_acc("main"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen=None, console=console)
    out = console.file.getvalue()
    assert "5h" in out
    assert "week" in out


def test_pct_color_returns_gradient_colour_string() -> None:
    """_pct_color returns an rgb(...) string for non-zero pct."""
    from claude_rotate.dashboard import _pct_color

    colour = _pct_color(50.0, width=12)
    assert colour.startswith("rgb(")


def test_pct_color_zero_returns_grey() -> None:
    from claude_rotate.dashboard import _pct_color

    assert _pct_color(0.0) == "grey50"


def test_pct_color_none_returns_grey() -> None:
    from claude_rotate.dashboard import _pct_color

    assert _pct_color(None) == "grey50"


# ---------------------------------------------------------------------------
# render_stale_footer
# ---------------------------------------------------------------------------


def _acc_oauth(name: str, *, age_days: int | None) -> Account:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    refreshed = (now - timedelta(days=age_days)) if age_days is not None else None
    return Account(
        name=name,
        runtime_token="sk-ant-oat01-" + "a" * 96,
        refresh_token="sk-ant-ort01-" + "r" * 40,
        label=name,
        created_at=now,
        plan="max_20x",
        metadata_refreshed_at=refreshed,
    )


def test_stale_footer_warns_when_stale(monkeypatch: object) -> None:
    from claude_rotate.dashboard import render_stale_footer

    now = datetime(2026, 4, 22, tzinfo=UTC)
    rows = [_row(_acc_oauth("work", age_days=13))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_stale_footer(rows, console=console, now=now)
    out = console.file.getvalue()
    assert "work" in out
    assert "13d" in out


def test_stale_footer_silent_when_fresh() -> None:
    from claude_rotate.dashboard import render_stale_footer

    now = datetime(2026, 4, 22, tzinfo=UTC)
    rows = [_row(_acc_oauth("work", age_days=2))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_stale_footer(rows, console=console, now=now)
    assert console.file.getvalue() == ""


def test_stale_footer_silent_for_ci_account() -> None:
    """CI accounts (refresh_token=None) must never trigger the stale footer."""
    from claude_rotate.dashboard import render_stale_footer

    now = datetime(2026, 4, 22, tzinfo=UTC)
    ci = Account(
        name="ci",
        runtime_token="sk-ant-oat01-" + "a" * 96,
        refresh_token=None,  # CI path
        label="ci",
        created_at=now,
        plan="unknown",
        metadata_refreshed_at=None,
    )
    rows = [_row(ci)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_stale_footer(rows, console=console, now=now)
    assert console.file.getvalue() == ""


def test_compute_forecast_5h_screenshot_value() -> None:
    # 63% bei noch 47m Reset im 5h-Fenster (Statusline-Referenzwert → 74)
    from claude_rotate.config import FORECAST_WINDOW_5H_SECONDS

    assert compute_forecast(63.0, 47 * 60, FORECAST_WINDOW_5H_SECONDS) == 74


def test_compute_forecast_weekly_screenshot_value() -> None:
    # 55% bei noch 1d14h47m Reset im 7d-Fenster (Statusline-Referenzwert → 71)
    from claude_rotate.config import FORECAST_WINDOW_7D_SECONDS

    reset = (24 + 14) * 3600 + 47 * 60  # 1d14h47m
    assert compute_forecast(55.0, reset, FORECAST_WINDOW_7D_SECONDS) == 71


def test_compute_forecast_truncates_like_statusline() -> None:
    # 74.70 muss zu 74 abgeschnitten werden (nicht gerundet auf 75)
    from claude_rotate.config import FORECAST_WINDOW_5H_SECONDS

    assert compute_forecast(63.0, 47 * 60, FORECAST_WINDOW_5H_SECONDS) == 74


def test_compute_forecast_none_pct_returns_none() -> None:
    assert compute_forecast(None, 3600, 18000) is None


def test_compute_forecast_no_active_window_returns_none() -> None:
    # reset_secs <= 0 → Fenster abgelaufen, keine Prognose
    assert compute_forecast(50.0, 0, 18000) is None


def test_compute_forecast_fresh_window_returns_none() -> None:
    # elapsed <= 0 (reset_secs == window) → 0 verstrichen, keine Prognose
    assert compute_forecast(50.0, 18000, 18000) is None


def test_compute_forecast_zero_pct_returns_zero() -> None:
    assert compute_forecast(0.0, 3600, 18000) == 0


def test_compute_forecast_caps_at_999() -> None:
    # Winziges elapsed → riesige Hochrechnung, gedeckelt
    assert compute_forecast(50.0, 18000 - 60, 18000) == 999


def test_compute_limit_eta_seconds_until_100() -> None:
    # 60% bei halb verstrichenem 5h-Fenster (reset=9000, elapsed=9000):
    # Prognose 120% → Limit wird erreicht in (100-60)*9000//60 = 6000s, vor Reset.
    assert compute_limit_eta(60.0, 9000, 18000) == 6000


def test_compute_limit_eta_none_when_forecast_under_100() -> None:
    # 63% bei noch 47m Reset → Prognose 74% < 100 → Fenster resettet vor der Wand.
    assert compute_limit_eta(63.0, 47 * 60, 18000) is None


def test_compute_limit_eta_zero_pct_returns_none() -> None:
    assert compute_limit_eta(0.0, 9000, 18000) is None


def test_compute_limit_eta_at_or_over_limit_returns_none() -> None:
    assert compute_limit_eta(100.0, 9000, 18000) is None


def test_compute_limit_eta_fresh_window_returns_none() -> None:
    # elapsed <= 0 (reset_secs == window) → keine verstrichene Zeit, keine ETA.
    assert compute_limit_eta(50.0, 18000, 18000) is None


def test_compute_limit_eta_no_active_window_returns_none() -> None:
    assert compute_limit_eta(50.0, 0, 18000) is None


def test_compute_limit_eta_none_pct_returns_none() -> None:
    assert compute_limit_eta(None, 9000, 18000) is None


def test_render_shows_forecast_bracket_by_default() -> None:
    # 50% bei noch 1h (3600s) im 5h-Fenster: elapsed=14400 → 50*18000//14400 = 62
    rows = [_row(_acc("main"), h5_pct=50.0, h5_secs=3600, w7_pct=20.0, w7_secs=86400)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="main", console=console)
    out = console.file.getvalue()
    assert "→62%" in out


def test_render_omits_forecast_when_disabled() -> None:
    rows = [_row(_acc("main"), h5_pct=50.0, h5_secs=3600)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="main", console=console, show_forecast=False)
    out = console.file.getvalue()
    assert "→" not in out


def test_render_no_forecast_for_elapsed_window() -> None:
    # reset_secs == window → frisches Fenster, keine Prognose, aber kein Crash
    rows = [_row(_acc("main"), h5_pct=10.0, h5_secs=18000, w7_pct=10.0, w7_secs=604800)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="main", console=console)
    out = console.file.getvalue()
    assert "→" not in out


def test_render_forecast_keeps_expires_column_aligned() -> None:
    # Eine Zeile mit Prognose, eine ohne (frisches Fenster) — die expires-Spalte
    # (Tag-Werte) muss in beiden Zeilen an derselben Spalte enden.
    fixed_now = datetime(2026, 4, 22, tzinfo=UTC)
    rows = [
        _row(_acc("a", sub_days=30), h5_pct=50.0, h5_secs=3600, w7_pct=20.0, w7_secs=86400),
        _row(_acc("b", sub_days=19), h5_pct=10.0, h5_secs=18000, w7_pct=10.0, w7_secs=604800),
    ]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="a", console=console, now=fixed_now)
    lines = [
        ln
        for ln in console.file.getvalue().splitlines()
        if "d" in ln and ("30d" in ln or "19d" in ln)
    ]
    assert len(lines) == 2
    # rstrip-Länge identisch → beide Zeilen enden an derselben Spalte
    assert len(lines[0].rstrip()) == len(lines[1].rstrip())


def test_forecast_enabled_default_true(monkeypatch) -> None:
    from claude_rotate.dashboard import forecast_enabled

    monkeypatch.delenv("CLAUDE_ROTATE_FORECAST", raising=False)
    assert forecast_enabled() is True


def test_forecast_enabled_off_when_zero(monkeypatch) -> None:
    from claude_rotate.dashboard import forecast_enabled

    monkeypatch.setenv("CLAUDE_ROTATE_FORECAST", "0")
    assert forecast_enabled() is False


def test_forecast_enabled_on_for_other_values(monkeypatch) -> None:
    from claude_rotate.dashboard import forecast_enabled

    monkeypatch.setenv("CLAUDE_ROTATE_FORECAST", "1")
    assert forecast_enabled() is True
    monkeypatch.setenv("CLAUDE_ROTATE_FORECAST", "yes")
    assert forecast_enabled() is True


def test_status_json_includes_forecast_fields() -> None:
    from claude_rotate.dashboard import status_json

    # 50% bei noch 1h im 5h-Fenster → 62; 20% bei noch 1d im 7d-Fenster
    rows = [_row(_acc("main"), h5_pct=50.0, h5_secs=3600, w7_pct=20.0, w7_secs=86400)]
    payload = status_json(rows, chosen="main")
    acct = payload["accounts"][0]
    assert acct["h5_forecast_pct"] == 62
    # 7d: elapsed = 604800 - 86400 = 518400 → 20*604800//518400 = 23
    assert acct["w7_forecast_pct"] == 23


def test_status_json_forecast_null_for_error_rows() -> None:
    from claude_rotate.dashboard import status_json

    rows = [_row(_acc("main"), h5_pct=None, w7_pct=None, status="relogin")]
    acct = status_json(rows, chosen=None)["accounts"][0]
    assert acct["h5_forecast_pct"] is None
    assert acct["w7_forecast_pct"] is None


def test_compute_forecast_hidden_at_or_above_100pct() -> None:
    # At/over the limit the projection is noise — suppress it.
    assert compute_forecast(100.0, 3600, 18000) is None
    assert compute_forecast(101.0, 3600, 18000) is None


def test_compute_forecast_shown_just_below_100pct() -> None:
    # 99% bei noch 1h im 5h-Fenster: elapsed=14400 → 99*18000//14400 = 123
    assert compute_forecast(99.0, 3600, 18000) == 123


def test_render_omits_forecast_when_already_maxed() -> None:
    # Beide Fenster >= 100% → kein Prognose-Token (Warnings dürfen "→" enthalten)
    import re

    rows = [_row(_acc("main"), h5_pct=100.0, h5_secs=3600, w7_pct=101.0, w7_secs=86400)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="main", console=console)
    assert not re.search(r"→\d+%", console.file.getvalue())


def test_status_json_forecast_null_when_maxed() -> None:
    from claude_rotate.dashboard import status_json

    rows = [_row(_acc("main"), h5_pct=100.0, h5_secs=3600, w7_pct=101.0, w7_secs=86400)]
    acct = status_json(rows, chosen="main")["accounts"][0]
    assert acct["h5_forecast_pct"] is None
    assert acct["w7_forecast_pct"] is None


# ---------------------------------------------------------------------------
# Responsive dashboard: status line, limit ETA, cards mode, risk footer
# ---------------------------------------------------------------------------


def test_render_status_line_mentions_active_and_rotation() -> None:
    rows = [_row(_acc("a")), _row(_acc("b"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen="b", active="a", console=console)
    out = console.file.getvalue()
    assert "Session runs on 'a' (@)" in out
    assert "rotates to 'b'" in out


def test_render_active_account_gets_at_marker() -> None:
    rows = [_row(_acc("a")), _row(_acc("b"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen="b", active="a", console=console)
    lines = console.file.getvalue().splitlines()
    # The name column sits inside the table border, so strip the border rule.
    assert any(ln.lstrip("│ ").startswith("@") and " a" in ln for ln in lines)


def test_render_shows_absolute_reset_clock() -> None:
    # 50% bei noch 1h im 5h-Fenster → Reset-Uhrzeit (HH:MM) muss erscheinen
    fixed_now = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
    expected = (fixed_now + timedelta(hours=1)).astimezone().strftime("%H:%M")
    rows = [_row(_acc("main"), h5_pct=50.0, h5_secs=3600)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="main", console=console, now=fixed_now)
    assert expected in console.file.getvalue()


def test_render_shows_limit_eta_when_wall_before_reset() -> None:
    # 60% bei halb verstrichenem 5h-Fenster → ETA nach 6000s, vor dem Reset.
    fixed_now = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)
    eta_clock = (fixed_now + timedelta(seconds=6000)).astimezone().strftime("%H:%M")
    rows = [_row(_acc("main"), h5_pct=60.0, h5_secs=9000)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="main", console=console, now=fixed_now)
    out = console.file.getvalue()
    assert "→120%" in out
    assert eta_clock in out


def test_render_plan_badge_shown() -> None:
    rows = [_row(_acc("main"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen="main", console=console)
    assert "Max-20" in console.file.getvalue()


def test_render_sub_column_shows_absolute_date() -> None:
    fixed_now = datetime(2026, 4, 22, tzinfo=UTC)
    rows = [_row(_acc("main", sub_days=30))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen="main", console=console, now=fixed_now)
    expected = (fixed_now + timedelta(days=30)).astimezone().strftime("%d %b")
    assert expected in console.file.getvalue()


def test_render_narrow_terminal_folds_into_cards() -> None:
    rows = [_row(_acc("main"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=60)
    render_dashboard(rows, chosen="main", console=console)
    out = console.file.getvalue()
    # Cards carry their own per-window labels and the plan in the header line
    assert "main · Max-20" in out
    assert "5h" in out
    assert "week" in out


def test_render_cards_mode_shows_error_status() -> None:
    rows = [_row(_acc("main"), h5_pct=None, w7_pct=None, status="relogin", note="token invalid")]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=60)
    render_dashboard(rows, chosen=None, console=console)
    out = console.file.getvalue()
    assert "RELOGIN" in out
    assert "token invalid" in out


def test_render_no_risk_footer_when_healthy() -> None:
    rows = [_row(_acc("main"), h5_pct=10.0, w7_pct=20.0)]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="main", console=console)
    out = console.file.getvalue()
    assert "⚠" not in out
    assert "Fallback" not in out


def test_footer_keeps_relogin_drops_quota_risk_and_fallback() -> None:
    # Heavy weekly usage but status ok → no quota-risk line; the relogin account
    # still surfaces as an action-needed warning; no fallback recommendation.
    rows = [
        _row(_acc("a"), w7_pct=95.0, w7_secs=86400),
        _row(_acc("dead"), h5_pct=None, w7_pct=None, status="relogin", note="token invalid"),
    ]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=160)
    render_dashboard(rows, chosen="a", active="a", console=console)
    out = console.file.getvalue()
    assert "at risk" not in out
    assert "Fallback" not in out
    assert "dead: relogin" in out


def test_status_json_includes_active() -> None:
    from claude_rotate.dashboard import status_json

    rows = [_row(_acc("main"))]
    payload = status_json(rows, chosen="main", active="main")
    assert payload["active"] == "main"
    payload = status_json(rows, chosen="main")
    assert payload["active"] is None


# ---------------------------------------------------------------------------
# Bordered table + dimmed unusable accounts
# ---------------------------------------------------------------------------


def test_render_table_draws_borders_and_row_rules() -> None:
    rows = [_row(_acc("a")), _row(_acc("b"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen="a", console=console)
    out = console.file.getvalue()
    assert "│" in out  # vertical rules
    assert "─" in out  # horizontal rules
    assert "├" in out  # a rule separates the two account rows


def test_is_unusable_when_5h_window_full() -> None:
    from claude_rotate.dashboard import is_unusable

    now = datetime(2026, 4, 22, tzinfo=UTC)
    assert is_unusable(_row(_acc("a"), h5_pct=100.0, w7_pct=20.0), now=now)
    assert is_unusable(_row(_acc("a"), h5_pct=102.0, w7_pct=20.0), now=now)


def test_is_unusable_when_weekly_window_full() -> None:
    from claude_rotate.dashboard import is_unusable

    now = datetime(2026, 4, 22, tzinfo=UTC)
    assert is_unusable(_row(_acc("a"), h5_pct=10.0, w7_pct=100.0), now=now)


def test_is_unusable_when_subscription_expired() -> None:
    from claude_rotate.dashboard import is_unusable

    acc = _acc("a", sub_days=5)
    now = datetime(2026, 4, 22, tzinfo=UTC) + timedelta(days=6)
    assert is_unusable(_row(acc, h5_pct=10.0, w7_pct=20.0), now=now)


def test_is_unusable_false_for_healthy_account() -> None:
    from claude_rotate.dashboard import is_unusable

    now = datetime(2026, 4, 22, tzinfo=UTC)
    assert not is_unusable(_row(_acc("a"), h5_pct=86.0, w7_pct=10.0), now=now)
    assert not is_unusable(_row(_acc("a"), h5_pct=None, w7_pct=None, status="relogin"), now=now)


def test_render_greys_out_unusable_row() -> None:
    # ANSI capture: the exhausted account's row is flattened to uniform grey —
    # no truecolor gradient (38;2;…) survives; the healthy row keeps its colours.
    rows = [
        _row(_acc("dead"), h5_pct=102.0, w7_pct=25.0),
        _row(_acc("fresh"), h5_pct=10.0, w7_pct=20.0),
    ]
    console = Console(file=StringIO(), force_terminal=True, color_system="truecolor", width=120)
    render_dashboard(rows, chosen="fresh", console=console)
    out = console.file.getvalue()
    table_lines = [ln for ln in out.splitlines() if "│" in ln]
    dead_line = next(ln for ln in table_lines if "dead" in ln)
    fresh_line = next(ln for ln in table_lines if "fresh" in ln)
    assert "38;2;" not in dead_line  # gradient colours stripped
    assert "38;2;" in fresh_line  # healthy row keeps the gradient bar
    assert "38;5;240" in dead_line  # grey35 applied
