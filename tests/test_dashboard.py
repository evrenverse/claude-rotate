from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO

from rich.console import Console
from rich.text import Text

from claude_rotate.accounts import Account
from claude_rotate.dashboard import DashboardRow, gradient_bar, render_dashboard


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


def test_render_contains_label_and_percentages() -> None:
    rows = [_row(_acc("main"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen=rows[0].account.name, console=console)
    out = console.file.getvalue()
    assert "Main" in out
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


def test_render_header_contains_5h_and_weekly() -> None:
    """The header row must mention '5h' and 'weekly'."""
    rows = [_row(_acc("main"))]
    console = Console(file=StringIO(), force_terminal=False, no_color=True, width=120)
    render_dashboard(rows, chosen=None, console=console)
    out = console.file.getvalue()
    assert "5h" in out
    assert "weekly" in out


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
