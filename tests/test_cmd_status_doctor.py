from __future__ import annotations

import json
from datetime import UTC, datetime
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

    A 429 on ``/oauth/usage`` is an API rate-limit on the probe endpoint,
    not a subscription-quota exhaustion. Without cached usage numbers we
    cannot reason about the account and surface that honestly.
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
    now = datetime(2026, 4, 22, tzinfo=UTC)
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
