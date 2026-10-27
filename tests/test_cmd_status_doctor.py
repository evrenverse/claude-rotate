from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def _acc(name: str = "main", *, ci_installed: bool = False) -> Account:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    return Account(
        name=name,
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label=name,
        created_at=now,
        plan="max_20x",
        email=f"{name}@example.com",
    )


def test_status_exits_3_when_no_accounts(tmp_path, capsys) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    from claude_rotate.commands import status

    rc = status.execute(p, as_json=False)
    assert rc == 3


def test_status_exits_0_when_all_healthy(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=_acc(), h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with patch("claude_rotate.commands.status.probe_many", return_value=[cand]):
        from claude_rotate.commands import status

        rc = status.execute(p, as_json=False)
    assert rc == 0


def test_status_persists_live_scoped_fetch(tmp_path) -> None:
    """A successful scoped fetch during status lands in the usage cache."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    from claude_rotate.selection import Candidate, ScopedLimit
    from claude_rotate.usage_cache import UsageCache

    cand = Candidate(
        account=_acc(),
        h5_pct=10.0,
        w7_pct=20.0,
        h5_reset_secs=3600,
        w7_reset_secs=86400,
        w7_scoped=(ScopedLimit(label="fable", pct=88.0, reset_secs=86400),),
    )
    with patch("claude_rotate.commands.status.probe_many", return_value=[cand]):
        from claude_rotate.commands import status

        rc = status.execute(p, as_json=False)
    assert rc == 0
    served = UsageCache(p).load_scoped("main")
    assert len(served) == 1
    assert served[0].label == "fable"
    assert served[0].pct == 88.0
    assert served[0].stale is True  # cache-served values are stale by definition


def test_status_json_outputs_valid_json(tmp_path, capsys) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=_acc(), h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with patch("claude_rotate.commands.status.probe_many", return_value=[cand]):
        from claude_rotate.commands import status

        status.execute(p, as_json=True)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["chosen"] == "main"


def test_status_json_reports_live_sessions(tmp_path, capsys) -> None:
    """End-to-end: status --json surfaces the live-session count from the registry."""
    import os
    import time

    from claude_rotate import sessions

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"matri": _acc("matri"), "ply": _acc("ply")})

    # Write a LIVE session record for "matri" backed by this process, so
    # count_load counts it instead of reaping it. last_active=now keeps it
    # inside the active window (active=1, idle=0).
    pid = os.getpid()
    now = time.time()
    sessions.write_record(
        p,
        sessions.SessionRecord(
            uuid="s1",
            account="matri",
            pid=pid,
            start_time=sessions.process_start_time(pid) or 0.0,
            started_at=now,
            last_active=now,
            claude_session_id="s-1",
        ),
    )

    from claude_rotate.selection import Candidate

    cands = [
        Candidate(
            account=_acc(name),
            h5_pct=10.0,
            w7_pct=20.0,
            h5_reset_secs=3600,
            w7_reset_secs=86400,
        )
        for name in ("matri", "ply")
    ]
    with patch("claude_rotate.commands.status.probe_many", return_value=cands):
        from claude_rotate.commands import status

        status.execute(p, as_json=True)

    out = json.loads(capsys.readouterr().out)
    by_name = {a["name"]: a for a in out["accounts"]}
    assert by_name["matri"]["sessions"] == {"active": 1, "idle": 0}
    assert by_name["matri"]["claude_sessions"] == ["s-1"]
    assert by_name["ply"]["claude_sessions"] == []


def test_doctor_green_when_all_ok(tmp_path, capsys) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    Store(p).save({"main": _acc()})

    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_ok),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)
    assert rc == 0
    out = capsys.readouterr().err
    assert "✓" in out


# ---------------------------------------------------------------------------
# Issue 1: status correctly classifies probe errors
# ---------------------------------------------------------------------------


def test_status_exits_0_when_all_accounts_rate_limited(tmp_path) -> None:
    """429 rate-limited → no RELOGIN, exit 0 is NOT expected when all are unusable."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"work": _acc("work")})

    from claude_rotate.selection import Candidate

    # Candidate with probe_error=rate_limited, no usage data
    cand = Candidate(
        account=_acc("work"),
        h5_pct=None,
        w7_pct=None,
        h5_reset_secs=0,
        w7_reset_secs=0,
        probe_error="rate_limited",
    )
    with patch("claude_rotate.commands.status.probe_many", return_value=[cand]):
        from claude_rotate.commands import status

        rc = status.execute(p, as_json=False)
    # No relogin → relogin_count == 0, but no resolved either → exit 2
    assert rc == 2


def test_status_rate_limited_without_cache_shows_no_data(tmp_path, capsys) -> None:
    """rate_limited probe with no cached usage → no_data row explaining why.

    A 429 without usable rate-limit headers gives us no trustworthy quota
    data. Without cached usage numbers we cannot reason about the account
    and surface that honestly.
    """
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"work": _acc("work")})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=_acc("work"),
        h5_pct=None,
        w7_pct=None,
        h5_reset_secs=0,
        w7_reset_secs=0,
        probe_error="rate_limited",
    )
    with patch("claude_rotate.commands.status.probe_many", return_value=[cand]):
        from claude_rotate.commands import status

        status.execute(p, as_json=True)

    out = json.loads(capsys.readouterr().out)
    assert out["accounts"][0]["status"] == "no_data"
    assert "rate-limited" in out["accounts"][0]["note"].lower()


def test_status_exits_2_for_unauthorized(tmp_path, capsys) -> None:
    """unauthorized probe → classified as RELOGIN, contributes to exit 2."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"work": _acc("work")})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=_acc("work"),
        h5_pct=None,
        w7_pct=None,
        h5_reset_secs=0,
        w7_reset_secs=0,
        probe_error="unauthorized",
    )
    with patch("claude_rotate.commands.status.probe_many", return_value=[cand]):
        from claude_rotate.commands import status

        rc = status.execute(p, as_json=True)

    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["accounts"][0]["status"] == "relogin"


# ---------------------------------------------------------------------------
# Issue 3: doctor uses probe_usage for CI-installed accounts
# ---------------------------------------------------------------------------


def test_doctor_ci_account_uses_probe_usage(tmp_path, capsys) -> None:
    """CI-installed account (refresh_token=None) → probe_usage, not fetch_profile."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    Store(p).save({"ci": _acc("ci", ci_installed=True)})

    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_ok),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    assert rc == 0
    err = capsys.readouterr().err
    assert "✓" in err


def test_doctor_ci_account_unauthorized_is_hard_error(tmp_path, capsys) -> None:
    """CI-installed account with 401 → hard error (exit 2)."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    Store(p).save({"ci": _acc("ci", ci_installed=True)})

    from claude_rotate.probe import ProbeResult

    probe_fail = ProbeResult(ok=False, http_code=401, error="unauthorized")
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_fail),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    assert rc == 2
    err = capsys.readouterr().err
    assert "REJECTED" in err


def test_doctor_ci_account_rate_limited_is_warning(tmp_path, capsys) -> None:
    """CI-installed account returning 429 → warning, not hard error (exit 1)."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    Store(p).save({"ci": _acc("ci", ci_installed=True)})

    from claude_rotate.probe import ProbeResult

    probe_rl = ProbeResult(ok=False, http_code=429, error="rate_limited")
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_rl),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    assert rc == 1
    err = capsys.readouterr().err
    assert "quota limit" in err


# ---------------------------------------------------------------------------
# Doctor subscription status display
# ---------------------------------------------------------------------------


def test_doctor_shows_active_subscription_status(tmp_path, capsys) -> None:
    """doctor shows ✓ subscription <name> active for active accounts."""
    from dataclasses import replace

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    acc = replace(_acc("main"), subscription_status="active")
    Store(p).save({"main": acc})

    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_ok),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    err = capsys.readouterr().err
    assert "subscription" in err
    assert "active" in err
    assert rc == 0


def test_doctor_shows_canceled_subscription_with_end_date(tmp_path, capsys) -> None:
    """doctor warns about canceled subscriptions with end date."""
    from dataclasses import replace
    from datetime import UTC, datetime

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    end = datetime(2026, 5, 15, tzinfo=UTC)
    acc = replace(_acc("legacy"), subscription_status="canceled", subscription_expires_at=end)
    Store(p).save({"legacy": acc})

    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_ok),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    err = capsys.readouterr().err
    assert "canceled" in err
    assert "2026-05-15" in err
    assert rc == 1  # warning


def test_doctor_skips_subscription_display_when_status_unknown(tmp_path, capsys) -> None:
    """doctor does not show subscription line when subscription_status is None."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    # _acc() default has subscription_status=None
    Store(p).save({"ci": _acc("ci")})

    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_ok),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    err = capsys.readouterr().err
    # No subscription status line should appear (subscription_status is None)
    assert "subscription ci" not in err
    assert rc == 0


# ---------------------------------------------------------------------------
# Stale-metadata warnings (doctor + list)
# ---------------------------------------------------------------------------


def test_doctor_warns_stale_oauth_account(tmp_path, capsys) -> None:
    """OAuth account with metadata_refreshed_at > 10d ago → ⚠ stale warning (exit 1)."""
    from dataclasses import replace
    from datetime import timedelta

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    now = datetime(2026, 4, 22, tzinfo=UTC)
    stale = replace(
        _acc("main"),
        refresh_token="sk-ant-ort01-" + "r" * 40,
        metadata_refreshed_at=now - timedelta(days=13),
    )
    Store(p).save({"main": stale})

    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_ok),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    err = capsys.readouterr().err
    assert "stale" in err
    assert rc == 1  # warning


def test_doctor_fresh_oauth_account_no_stale_warning(tmp_path, capsys) -> None:
    """OAuth account refreshed recently → ✓ fresh, no stale warning."""
    from dataclasses import replace

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    # Date-independent: "refreshed recently" must stay within the stale window
    # regardless of the real calendar date when the suite runs.
    now = datetime.now(UTC) - timedelta(days=1)
    fresh = replace(
        _acc("main"),
        refresh_token="sk-ant-ort01-" + "r" * 40,
        metadata_refreshed_at=now,
        refresh_token_obtained_at=now,
    )
    Store(p).save({"main": fresh})

    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_ok),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    err = capsys.readouterr().err
    assert "stale" not in err
    assert "fresh" in err
    assert rc == 0


def test_doctor_ci_account_no_stale_warning(tmp_path, capsys) -> None:
    """CI-installed account (refresh_token=None) → no staleness check at all."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True, mode=0o700)
    # _acc() has refresh_token=None (CI path)
    Store(p).save({"ci": _acc("ci")})

    from claude_rotate.probe import ProbeResult

    probe_ok = ProbeResult(
        ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with (
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/usr/bin/claude",
        ),
        patch("claude_rotate.commands.doctor.fetch_usage", return_value=probe_ok),
    ):
        from claude_rotate.commands import doctor

        rc = doctor.execute(p)

    err = capsys.readouterr().err
    assert "stale" not in err
    assert "metadata" not in err
    assert rc == 0


def test_list_shows_stale_suffix_for_oauth_account(tmp_path, capsys) -> None:
    """list shows '⚠ stale Nd' suffix for OAuth accounts not refreshed in >10d."""
    from dataclasses import replace
    from datetime import timedelta

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    now = datetime(2026, 4, 22, tzinfo=UTC)
    stale = replace(
        _acc("main"),
        refresh_token="sk-ant-ort01-" + "r" * 40,
        metadata_refreshed_at=now - timedelta(days=13),
    )
    Store(p).save({"main": stale})

    from claude_rotate.commands import list_cmd

    list_cmd.execute(p)
    err = capsys.readouterr().err
    assert "stale" in err


def test_list_no_stale_suffix_for_ci_account(tmp_path, capsys) -> None:
    """list does NOT show stale suffix for CI accounts (no refresh_token)."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"ci": _acc("ci")})

    from claude_rotate.commands import list_cmd

    list_cmd.execute(p)
    err = capsys.readouterr().err
    assert "stale" not in err


def test_doctor_warns_on_stale_refresh_token(tmp_path, monkeypatch, capsys) -> None:
    from datetime import timedelta

    from claude_rotate.commands.doctor import execute
    from claude_rotate.probe import ProbeResult

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)

    now = datetime.now(UTC)
    acct = Account(
        name="old",
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label="Max-20 old",
        created_at=now,
        refresh_token="sk-ant-ort01-" + "b" * 96,
        refresh_token_obtained_at=now - timedelta(days=20),  # very stale
        runtime_token_obtained_at=now,
        metadata_refreshed_at=now,
    )
    Store(p).save({"old": acct})

    with (
        patch(
            "claude_rotate.commands.doctor.fetch_usage",
            return_value=ProbeResult(
                ok=True,
                http_code=200,
                h5_pct=0.0,
                w7_pct=0.0,
                h5_reset_secs=0,
                w7_reset_secs=0,
            ),
        ),
        patch(
            "claude_rotate.commands.doctor.resolve_claude_binary",
            return_value="/fake/claude",
        ),
    ):
        execute(p)

    err = capsys.readouterr().err
    assert "refresh_token" in err.lower() or "refresh" in err.lower()
    assert "stale" in err.lower() or "20d" in err


def test_status_does_not_choose_disabled_account(tmp_path, capsys) -> None:
    """A disabled account never receives the chosen (>) marker, even with best quota."""
    from dataclasses import replace

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    enabled = _acc("on")
    disabled = replace(_acc("off"), disabled=True)
    Store(p).save({"on": enabled, "off": disabled})

    from claude_rotate.selection import Candidate

    # Disabled "off" has the freshest quota, but must not be chosen.
    cands = [
        Candidate(
            account=enabled, h5_pct=50.0, w7_pct=50.0, h5_reset_secs=3600, w7_reset_secs=86400
        ),
        Candidate(
            account=disabled, h5_pct=5.0, w7_pct=5.0, h5_reset_secs=3600, w7_reset_secs=86400
        ),
    ]
    with patch("claude_rotate.commands.status.probe_many", return_value=cands):
        from claude_rotate.commands import status

        status.execute(p, as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert out["chosen"] == "on"
    by_name = {a["name"]: a for a in out["accounts"]}
    assert by_name["off"]["disabled"] is True


def test_status_all_disabled_has_no_chosen(tmp_path, capsys) -> None:
    """When every account is disabled, status still lists them but chooses none."""
    from dataclasses import replace

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    disabled = replace(_acc("a"), disabled=True)
    Store(p).save({"a": disabled})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=disabled, h5_pct=10.0, w7_pct=10.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    with patch("claude_rotate.commands.status.probe_many", return_value=[cand]):
        from claude_rotate.commands import status

        status.execute(p, as_json=True)
    out = json.loads(capsys.readouterr().out)
    assert out["chosen"] is None
    assert out["accounts"][0]["disabled"] is True


def test_status_forecast_env_off_passes_show_forecast_false(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=_acc(), h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    captured: dict[str, object] = {}

    def fake_render(rows, *, chosen, console, **kwargs):
        captured["show_forecast"] = kwargs.get("show_forecast")

    monkeypatch.setenv("CLAUDE_ROTATE_FORECAST", "0")
    monkeypatch.setattr("claude_rotate.commands.status.probe_many", lambda accts: [cand])
    monkeypatch.setattr("claude_rotate.commands.status.render_dashboard", fake_render)

    from claude_rotate.commands import status

    status.execute(p, as_json=False)
    assert captured["show_forecast"] is False


def test_status_forecast_env_default_on(tmp_path, monkeypatch) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=_acc(), h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )
    captured: dict[str, object] = {}

    def fake_render(rows, *, chosen, console, **kwargs):
        captured["show_forecast"] = kwargs.get("show_forecast")

    monkeypatch.delenv("CLAUDE_ROTATE_FORECAST", raising=False)
    monkeypatch.setattr("claude_rotate.commands.status.probe_many", lambda accts: [cand])
    monkeypatch.setattr("claude_rotate.commands.status.render_dashboard", fake_render)

    from claude_rotate.commands import status

    status.execute(p, as_json=False)
    assert captured["show_forecast"] is True


def test_status_watch_skips_live_loop_on_non_tty(tmp_path, monkeypatch) -> None:
    """``--watch`` under a pipe/capture must NOT enter the live loop — single render."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=_acc(), h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )

    def _boom(*args, **kwargs):
        raise AssertionError("_run_watch must not run when stderr is not a terminal")

    monkeypatch.setattr("claude_rotate.commands.status.probe_many", lambda accts: [cand])
    monkeypatch.setattr("claude_rotate.commands.status._run_watch", _boom)

    from claude_rotate.commands import status

    # pytest captures stderr → Console.is_terminal is False → falls through.
    rc = status.execute(p, as_json=False, watch=5.0)
    assert rc == 0


def test_run_watch_redraws_then_exits_on_keyboard_interrupt(tmp_path, monkeypatch) -> None:
    """The live loop probes + redraws each cycle, then exits cleanly on Ctrl-C."""
    import io

    from rich.console import Console

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    from claude_rotate.selection import Candidate

    cand = Candidate(
        account=_acc(), h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )

    probe_calls = {"n": 0}

    def _probe(_accts):
        probe_calls["n"] += 1
        return [cand]

    # Break out of the otherwise-infinite loop after the second frame.
    def _sleep(_secs):
        if probe_calls["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("claude_rotate.commands.status.probe_many", _probe)
    monkeypatch.setattr("claude_rotate.commands.status.time.sleep", _sleep)

    from claude_rotate.commands import status

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100)

    rc = status._run_watch(
        p, interval=5.0, as_json=False, report=False, console=console, here=tmp_path
    )

    assert rc == 0
    assert probe_calls["n"] >= 2  # redrew at least twice before Ctrl-C
    out = buf.getvalue()
    assert "refreshing every 5s" in out
    assert "Ctrl-C to quit" in out


def _healthy_candidate():
    from claude_rotate.selection import Candidate

    return Candidate(
        account=_acc(), h5_pct=10.0, w7_pct=20.0, h5_reset_secs=3600, w7_reset_secs=86400
    )


def test_status_shows_provider_quotas_under_the_dashboard(tmp_path, capsys) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})
    from claude_rotate.providers import ProviderQuota, ProviderWindow

    quota = ProviderQuota(
        provider="codex",
        account="team",
        windows=(ProviderWindow(label="5h", used_pct=42.0, reset_secs=3600),),
    )
    with (
        patch("claude_rotate.commands.status.probe_many", return_value=[_healthy_candidate()]),
        patch("claude_rotate.commands.status.collect_providers", return_value=[quota]),
    ):
        from claude_rotate.commands import status

        status.execute(p, as_json=False)

    assert "codex" in capsys.readouterr().err


def test_status_exit_code_ignores_a_broken_provider(tmp_path) -> None:
    """Exit codes describe Anthropic account health — nothing else may move them."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    with (
        patch("claude_rotate.commands.status.probe_many", return_value=[_healthy_candidate()]),
        patch(
            "claude_rotate.commands.status.collect_providers",
            side_effect=RuntimeError("provider exploded"),
        ),
    ):
        from claude_rotate.commands import status

        rc = status.execute(p, as_json=False)

    assert rc == 0


def test_status_json_includes_provider_quotas(tmp_path, capsys) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})
    from claude_rotate.providers import ProviderQuota, ProviderWindow

    quota = ProviderQuota(
        provider="gemini",
        account="Gemini Models",
        windows=(ProviderWindow(label="week", used_pct=51.0, reset_secs=86400),),
    )
    with (
        patch("claude_rotate.commands.status.probe_many", return_value=[_healthy_candidate()]),
        patch("claude_rotate.commands.status.collect_providers", return_value=[quota]),
    ):
        from claude_rotate.commands import status

        status.execute(p, as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["providers"][0]["account"] == "Gemini Models"


def test_status_report_includes_provider_quotas(tmp_path, capsys) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})
    from claude_rotate.providers import ProviderQuota, ProviderWindow

    quota = ProviderQuota(
        provider="codex",
        account="team",
        windows=(ProviderWindow(label="5h", used_pct=42.0, reset_secs=3600),),
    )
    with (
        patch("claude_rotate.commands.status.probe_many", return_value=[_healthy_candidate()]),
        patch("claude_rotate.commands.status.collect_providers", return_value=[quota]),
    ):
        from claude_rotate.commands import status

        status.execute(p, as_json=False, report=True)

    assert "codex" in capsys.readouterr().out


def test_status_never_shells_out_to_a_real_provider_tool(tmp_path, monkeypatch) -> None:
    """The suite must not depend on this machine's codex/agy installs.

    ``status`` collects third-party quota on every run, so without a guard
    every status test would spawn ``agy`` and wait seconds for it.
    """
    import subprocess

    # Recorded, not raised: the provider layer swallows exceptions by design,
    # so a raising stub would be caught and the test would pass regardless.
    calls: list[object] = []

    def record(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise FileNotFoundError  # behave like a tool that is not installed

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(subprocess, "Popen", record)

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    with patch("claude_rotate.commands.status.probe_many", return_value=[_healthy_candidate()]):
        from claude_rotate.commands import status

        assert status.execute(p, as_json=False) == 0

    assert calls == []


def test_status_aligns_the_provider_table_with_the_account_table(tmp_path, capsys) -> None:
    """Both tables are drawn one above the other; their edges must line up."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})
    from claude_rotate.providers import ProviderQuota, ProviderWindow

    quota = ProviderQuota(
        provider="codex",
        account="team",
        windows=(ProviderWindow(label="5h", used_pct=42.0, reset_secs=3600),),
    )
    with (
        patch("claude_rotate.commands.status.probe_many", return_value=[_healthy_candidate()]),
        patch("claude_rotate.commands.status.collect_providers", return_value=[quota]),
    ):
        from claude_rotate.commands import status

        status.execute(p, as_json=False)

    borders = {len(ln) for ln in capsys.readouterr().err.splitlines() if ln.startswith(("╭", "╰"))}
    assert len(borders) == 1, f"tables drawn at differing widths: {sorted(borders)}"


# --- account groups: --dir, allowed set, exit code, cache writes -------------


def _grouped_status(
    monkeypatch,
    tmp_path: Path,
    *,
    unauthorized: tuple[str, ...] = (),
    h5: float = 10.0,
):
    """Two accounts a, b with group k={a} bound to ``tmp_path/bound``; return (paths, bound dir).

    ``probe_many`` is patched: accounts in ``unauthorized`` fail with a 401, the rest
    probe at ``h5`` percent.
    """
    from claude_rotate.groups import Group
    from claude_rotate.selection import Candidate
    from claude_rotate.settings import RotateConfig, save_config

    monkeypatch.delenv("CLAUDE_ROTATE_SESSION_ISOLATION", raising=False)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})
    bound = (tmp_path / "bound").resolve()
    bound.mkdir()
    (tmp_path / "other").mkdir()
    save_config(
        p, RotateConfig(session_isolation=True, groups={"k": Group("k", ("a",), (str(bound),))})
    )

    def probe(accts):
        return [
            Candidate(
                account=a,
                h5_pct=None if a.name in unauthorized else h5,
                w7_pct=None if a.name in unauthorized else h5,
                h5_reset_secs=3600,
                w7_reset_secs=86400,
                probe_error="unauthorized" if a.name in unauthorized else "",
            )
            for a in accts
        ]

    monkeypatch.setattr("claude_rotate.commands.status.probe_many", probe)
    return p, bound


def test_status_json_dir_marks_allowed_and_chosen(tmp_path, monkeypatch, capsys) -> None:
    from claude_rotate.commands import status

    p, bound = _grouped_status(monkeypatch, tmp_path)
    status.execute(p, as_json=True, session_dir=bound)
    data = json.loads(capsys.readouterr().out)
    assert data["group"] == "k"
    assert data["dir"] == str(bound.resolve())
    assert data["chosen"] == "a"
    assert {x["name"]: x["allowed_here"] for x in data["accounts"]} == {"a": True, "b": False}
    assert {x["name"]: x["group"] for x in data["accounts"]} == {"a": "k", "b": None}


def test_status_json_elsewhere_excludes_the_bound_group(tmp_path, monkeypatch, capsys) -> None:
    from claude_rotate.commands import status

    p, _ = _grouped_status(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path / "other")  # no --dir: falls back to the cwd
    status.execute(p, as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["group"] is None
    assert data["dir"] == str((tmp_path / "other").resolve())
    assert data["chosen"] == "b"
    assert {x["name"]: x["allowed_here"] for x in data["accounts"]} == {"a": False, "b": True}


def test_status_exit_code_counts_only_allowed(tmp_path, monkeypatch) -> None:
    from claude_rotate.commands import status

    p, bound = _grouped_status(monkeypatch, tmp_path, unauthorized=("b",))
    # b needs a re-login but is not allowed in k's directory: a is healthy -> 0.
    assert status.execute(p, as_json=False, session_dir=bound) == 0
    # Anywhere else b is the only allowed account -> 2.
    assert status.execute(p, as_json=False, session_dir=tmp_path / "other") == 2


def test_status_exit_code_2_when_the_allowed_set_is_exhausted(tmp_path, monkeypatch) -> None:
    from claude_rotate.commands import status

    p, bound = _grouped_status(monkeypatch, tmp_path, h5=100.0)
    assert status.execute(p, as_json=False, session_dir=bound) == 2


def test_status_exit_code_2_when_the_allowed_accounts_are_all_disabled(
    tmp_path, monkeypatch
) -> None:
    """``run`` refuses such a group (exit 3); ``status`` must not report it healthy."""
    from dataclasses import replace

    from claude_rotate.commands import status

    p, bound = _grouped_status(monkeypatch, tmp_path)
    Store(p).save({"a": replace(_acc("a"), disabled=True), "b": _acc("b")})
    assert status.execute(p, as_json=False, session_dir=bound) == 2
    # Anywhere else the enabled b is allowed and healthy.
    assert status.execute(p, as_json=False, session_dir=tmp_path / "other") == 0


def test_status_dir_missing_is_error(tmp_path, monkeypatch, capsys) -> None:
    from claude_rotate.commands import status

    p, _ = _grouped_status(monkeypatch, tmp_path)
    rc = status.execute(p, as_json=True, session_dir=tmp_path / "nope")
    assert rc == 1
    captured = capsys.readouterr()
    assert "no such directory" in captured.err
    assert captured.out == ""


def test_status_saves_live_probe_to_cache(tmp_path, monkeypatch) -> None:
    from claude_rotate.commands import status
    from claude_rotate.usage_cache import UsageCache

    p, bound = _grouped_status(monkeypatch, tmp_path, h5=37.0)
    status.execute(p, as_json=False, session_dir=bound)
    cached = UsageCache(p).load("a")
    assert cached is not None
    assert cached.h5_pct == 37.0


def test_status_dashboard_shows_group_boxes_context_and_isolation_warning(
    tmp_path, monkeypatch
) -> None:
    from dataclasses import replace
    from io import StringIO

    from rich.console import Console

    from claude_rotate.commands import status
    from claude_rotate.dashboard import short_path

    p, bound = _grouped_status(monkeypatch, tmp_path)
    collected = status._collect(p, bound)

    def render(c: status._Collected) -> str:
        console = Console(file=StringIO(), width=160, force_terminal=False, color_system=None)
        status._render_dashboard(c, console)
        return console.file.getvalue()

    out = render(collected)
    shown = short_path(bound)  # ~-shortened when the tmp dir lives under $HOME
    assert f"{shown} · group k · next pick a" in out
    assert f" k · {shown}" in out
    assert " ungrouped · not allowed here" in out
    assert "session isolation" not in out
    assert "Account groups need session isolation" in render(
        replace(collected, isolation_missing=True)
    )


# --- status speed: instant cached frame, in-place live redraw ----------------

_REDRAW = re.compile(r"\x1b\[(\d+)F\x1b\[J")


def _tty_status(monkeypatch, tmp_path: Path, *, height: int, cached_ago: float = 0.0):
    """One account cached at 12 %, probing live at 55 %; stderr is a ``height``-row terminal.

    The cache entry is ``cached_ago`` seconds old. Returns ``(paths, console buffer, probes)``;
    ``probes`` holds what was already on the console each time ``probe_many`` ran.
    """
    import time

    from rich.console import Console

    from claude_rotate.probe import ProbeResult
    from claude_rotate.selection import Candidate
    from claude_rotate.usage_cache import UsageCache

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a")})
    saved_at = time.time() - cached_ago
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: saved_at)
        UsageCache(p).save(
            "a",
            ProbeResult(
                ok=True,
                http_code=200,
                h5_pct=12.0,
                w7_pct=12.0,
                h5_reset_secs=6 * 3600,
                w7_reset_secs=5 * 86400,
            ),
        )
    buf = io.StringIO()
    probes: list[str] = []

    def probe(accts):
        probes.append(buf.getvalue())
        return [
            Candidate(account=a, h5_pct=55.0, w7_pct=55.0, h5_reset_secs=3600, w7_reset_secs=86400)
            for a in accts
        ]

    monkeypatch.setattr("claude_rotate.commands.status.probe_many", probe)
    monkeypatch.setattr(
        "claude_rotate.commands.status._console",
        lambda: Console(file=buf, force_terminal=True, width=140, height=height),
    )
    return p, buf, probes


def test_status_tty_draws_cached_then_live(tmp_path, monkeypatch) -> None:
    from claude_rotate.commands import status

    p, buf, probes = _tty_status(monkeypatch, tmp_path, height=80)
    assert status.execute(p, as_json=False) == 0
    out = buf.getvalue()
    redraw = _REDRAW.search(out)
    assert redraw is not None
    assert out.index("12%") < redraw.start() < out.rindex("55%")
    assert "12%" not in out[redraw.end() :]
    assert "cached 0m ago" in out[: redraw.start()]
    # The erase rewinds exactly over the cached frame.
    assert int(redraw.group(1)) == out[: redraw.start()].count("\n")
    # The cached frame is already on screen while the probes run.
    assert len(probes) == 1
    assert "12%" in probes[0] and "cached" in probes[0]


def test_status_tty_first_frame_shows_a_two_hour_old_cache(tmp_path, monkeypatch) -> None:
    """Old entries still fill the first frame (marked cached) instead of reading ``no data``."""
    from claude_rotate.commands import status

    p, buf, probes = _tty_status(monkeypatch, tmp_path, height=80, cached_ago=7200)
    assert status.execute(p, as_json=False) == 0
    assert "12%" in probes[0]
    assert "cached 2h 0m ago" in probes[0]
    assert "no cached data yet" not in buf.getvalue()


def test_status_tty_tall_frame_skips_cached(tmp_path, monkeypatch) -> None:
    from claude_rotate.commands import status

    p, buf, probes = _tty_status(monkeypatch, tmp_path, height=5)
    assert status.execute(p, as_json=False) == 0
    out = buf.getvalue()
    assert "12%" not in out
    assert "55%" in out
    assert out.index("Probing") < out.index("\x1b[1F\x1b[J") < out.index("55%")
    assert len(probes) == 1 and "Probing" in probes[0]


def test_status_tty_without_accounts_skips_cached(tmp_path, monkeypatch, capsys) -> None:
    from claude_rotate.commands import status

    p, buf, _ = _tty_status(monkeypatch, tmp_path, height=80)
    Store(p).save({})
    assert status.execute(p, as_json=False) == 3
    # Only the one-line "Probing…" erase: there is no cached frame to rewind over.
    assert [m.group(1) for m in _REDRAW.finditer(buf.getvalue())] == ["1"]
    assert "No accounts configured" in capsys.readouterr().err


def test_status_dumb_terminal_skips_cached_frame(tmp_path, monkeypatch) -> None:
    """A dumb terminal cannot rewind: no cached frame and no cursor escapes."""
    from rich.console import Console

    from claude_rotate.commands import status

    p, buf, probes = _tty_status(monkeypatch, tmp_path, height=80)
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(
        "claude_rotate.commands.status._console",
        lambda: Console(file=buf, force_terminal=True, width=140, height=80),
    )
    assert status.execute(p, as_json=False) == 0
    out = buf.getvalue()
    assert "\x1b[" not in out
    assert "12%" not in out and "55%" in out
    assert probes == [""]  # nothing was on screen while probing


def test_status_json_never_uses_cached_frame(tmp_path, monkeypatch, capsys) -> None:
    from claude_rotate.commands import status

    p, buf, probes = _tty_status(monkeypatch, tmp_path, height=80)
    assert status.execute(p, as_json=True) == 0
    assert probes == [""]
    assert buf.getvalue() == ""
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert json.loads(out)["accounts"][0]["h5_pct"] == 55.0


def test_collect_cached_only_never_probes_or_writes(tmp_path, monkeypatch) -> None:
    import time

    from claude_rotate.commands import status
    from claude_rotate.usage_cache import UsageCache

    p, _, probes = _tty_status(monkeypatch, tmp_path, height=80)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})  # b has no cache entry

    def snapshot() -> dict[str, bytes]:
        return {f.name: f.read_bytes() for f in p.usage_dir.iterdir()}

    before = snapshot()
    monkeypatch.setattr(
        "claude_rotate.commands.status.refresh_stale_accounts",
        lambda _p: (_ for _ in ()).throw(AssertionError("no metadata refresh in cached_only")),
    )
    probed_at = UsageCache(p).probed_at("a")
    assert probed_at is not None

    c = status._collect(p, tmp_path, cached_only=True)

    assert probes == []
    assert snapshot() == before  # nothing written: no entry for b, a untouched, no history
    rows = {r.account.name: r for r in c.rows}
    assert rows["a"].h5_pct == 12.0 and rows["a"].from_cache
    assert rows["b"].status == "no_data" and rows["b"].note == "no cached data yet"
    assert c.relogin_count == 0  # "cached" is not a dead token
    assert c.cache_age_secs is not None
    assert abs(c.cache_age_secs - (time.time() - probed_at)) < 5
