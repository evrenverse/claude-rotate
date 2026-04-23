# tests/test_cmd_run.py
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from claude_rotate.accounts import Account, Store
from claude_rotate.commands.run import execute
from claude_rotate.config import Paths
from claude_rotate.selection import Candidate


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def _acc(name: str = "main", plan: str = "max_20x") -> Account:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    return Account(
        name=name,
        runtime_token=f"sk-ant-oat01-{name}" + "a" * 80,
        label=name,
        created_at=now,
        plan=plan,
    )


def test_run_with_no_accounts_returns_nonzero(tmp_path, capsys) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    rc = execute(p, [])
    assert rc == 3  # "no accounts" exit code
    assert "No accounts" in capsys.readouterr().err


def test_run_picks_account_and_execs_claude(tmp_path) -> None:
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    with (
        patch(
            "claude_rotate.commands.run.probe_many",
            return_value=[
                Candidate(
                    account=_acc(),
                    h5_pct=10.0,
                    w7_pct=10.0,
                    h5_reset_secs=3600,
                    w7_reset_secs=86400,
                )
            ],
        ),
        patch("claude_rotate.commands.run.reconcile_all") as mock_reconcile,
        patch("claude_rotate.commands.run.ensure_fresh", side_effect=lambda a, _p: a) as mock_fresh,
        patch("claude_rotate.commands.run.exec_claude") as mock_exec,
    ):
        execute(p, ["hello"])

    mock_reconcile.assert_called_once()
    mock_fresh.assert_called_once()
    mock_exec.assert_called_once()

    # exec_claude receives (Account, Paths, args). write_current_session now
    # lives inside exec_claude so cron never sees a stale breadcrumb.
    call_args = mock_exec.call_args[0]
    assert isinstance(call_args[0], Account)
    assert call_args[0].runtime_token.startswith("sk-ant-oat01-")
    assert call_args[1] is p
    assert call_args[2] == ["hello"]


def test_run_no_probe_data_still_calls_ensure_fresh(tmp_path) -> None:
    """Fallback path (no probe data) must also pre-refresh before exec."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})

    with (
        patch(
            "claude_rotate.commands.run.probe_many",
            return_value=[
                Candidate(
                    account=_acc(),
                    h5_pct=None,
                    w7_pct=None,
                    h5_reset_secs=0,
                    w7_reset_secs=0,
                    probe_error="network",
                )
            ],
        ),
        patch("claude_rotate.commands.run.reconcile_all"),
        patch("claude_rotate.commands.run.ensure_fresh", side_effect=lambda a, _p: a) as mock_fresh,
        patch("claude_rotate.commands.run.exec_claude") as mock_exec,
    ):
        execute(p, [])
    mock_fresh.assert_called_once()
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][1] is p  # exec_claude(account, paths, args)


def test_run_hints_legacy_credentials(tmp_path, monkeypatch, capsys) -> None:
    # A fake ~/.claude with legacy creds
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials-legacy.json").write_text("{}")
    monkeypatch.setenv("HOME", str(fake_home))

    p = _paths(tmp_path / "rot")
    p.config_dir.mkdir(parents=True)
    rc = execute(p, [])
    assert rc == 3
    err = capsys.readouterr().err
    assert "setup-token" in err.lower()
    assert ".credentials-" in err


def test_run_honours_pin(tmp_path) -> None:
    """Pin restricts the selection pool, not the dashboard.

    All accounts are probed (so the user sees the complete picture) but
    ``exec_claude`` is called with the pinned account's token regardless
    of whether a non-pinned account would score better.
    """
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    from dataclasses import replace

    a = _acc("a")
    b_pinned = replace(_acc("b"), pinned=True)
    Store(p).save({"a": a, "b": b_pinned})

    # Non-pinned "a" has fresher quota (5%) than pinned "b" (50%) — without
    # pinning the heuristic would pick "a". Pin forces "b".
    with (
        patch(
            "claude_rotate.commands.run.probe_many",
            return_value=[
                Candidate(
                    account=a,
                    h5_pct=5.0,
                    w7_pct=5.0,
                    h5_reset_secs=3600,
                    w7_reset_secs=86400,
                ),
                Candidate(
                    account=b_pinned,
                    h5_pct=50.0,
                    w7_pct=50.0,
                    h5_reset_secs=3600,
                    w7_reset_secs=86400,
                ),
            ],
        ) as probe,
        patch("claude_rotate.commands.run.reconcile_all"),
        patch("claude_rotate.commands.run.ensure_fresh", side_effect=lambda a, _p: a),
        patch("claude_rotate.commands.run.exec_claude") as mock_exec,
    ):
        execute(p, [])
    # Both accounts were probed — dashboard shows the full picture
    probed = probe.call_args[0][0]
    assert sorted(x.name for x in probed) == ["a", "b"]
    # But selection respected the pin → exec'd with the pinned Account
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0].name == "b"
    assert mock_exec.call_args[0][0].runtime_token == b_pinned.runtime_token
