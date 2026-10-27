# tests/test_cmd_run.py
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_rotate.accounts import Account, Store
from claude_rotate.commands.run import execute
from claude_rotate.config import Paths
from claude_rotate.groups import Group
from claude_rotate.selection import Candidate
from claude_rotate.settings import RotateConfig, save_config


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
    """Pin restricts the selection pool, not the probing.

    All allowed accounts are probed, but ``exec_claude`` is called with the
    pinned account's token regardless of whether a non-pinned account would
    score better.
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
    # Both accounts were probed
    probed = probe.call_args[0][0]
    assert sorted(x.name for x in probed) == ["a", "b"]
    # But selection respected the pin → exec'd with the pinned Account
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0].name == "b"
    assert mock_exec.call_args[0][0].runtime_token == b_pinned.runtime_token


def test_run_excludes_disabled_account(tmp_path) -> None:
    """A disabled account is never picked, even with the best quota."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    from dataclasses import replace

    a = _acc("a")
    b_disabled = replace(_acc("b"), disabled=True)
    Store(p).save({"a": a, "b": b_disabled})

    # Disabled "b" has the freshest quota (5%); enabled "a" is worse (50%).
    # Without the disable the heuristic would pick "b" — disable forces "a".
    with (
        patch("claude_rotate.commands.run.refresh_stale_accounts"),
        patch("claude_rotate.commands.run.refresh_stale_tokens", return_value=False),
        patch(
            "claude_rotate.commands.run.probe_many",
            return_value=[
                Candidate(
                    account=a,
                    h5_pct=50.0,
                    w7_pct=50.0,
                    h5_reset_secs=3600,
                    w7_reset_secs=86400,
                ),
                Candidate(
                    account=b_disabled,
                    h5_pct=5.0,
                    w7_pct=5.0,
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
    # Both accounts were probed
    probed = probe.call_args[0][0]
    assert sorted(x.name for x in probed) == ["a", "b"]
    # But selection excluded the disabled account
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0].name == "a"


def test_run_all_disabled_refuses(tmp_path, capsys) -> None:
    """When every account is disabled, run refuses (exit 3) and never execs."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    from dataclasses import replace

    Store(p).save(
        {
            "a": replace(_acc("a"), disabled=True),
            "b": replace(_acc("b"), disabled=True),
        }
    )
    with (
        patch("claude_rotate.commands.run.refresh_stale_accounts"),
        patch("claude_rotate.commands.run.reconcile_all"),
        patch("claude_rotate.commands.run.probe_many") as mock_probe,
        patch("claude_rotate.commands.run.exec_claude") as mock_exec,
    ):
        rc = execute(p, [])
    assert rc == 3
    assert "disabled" in capsys.readouterr().err.lower()
    mock_exec.assert_not_called()
    mock_probe.assert_not_called()  # short-circuits before probing


def test_run_reserves_session_and_passes_uuid(tmp_path) -> None:
    from claude_rotate import sessions

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
        patch("claude_rotate.commands.run.reconcile_all"),
        patch("claude_rotate.commands.run.ensure_fresh", side_effect=lambda a, _p: a),
        patch("claude_rotate.commands.run.exec_claude") as mock_exec,
    ):
        execute(p, [])

    # A reservation record was written for the chosen account…
    recs = sessions.read_records(p)
    assert len(recs) == 1
    assert recs[0].account == "main"
    # …and exec_claude received that record's uuid.
    assert mock_exec.call_args.kwargs["session_uuid"] == recs[0].uuid


def test_run_burst_fans_out_across_accounts(tmp_path) -> None:
    """Second run sees the first's reservation and avoids that account."""
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    Store(p).save({"a": _acc("a"), "b": _acc("b")})

    def two_equal(_pool):
        return [
            Candidate(
                account=_acc("a"), h5_pct=10.0, w7_pct=10.0, h5_reset_secs=3600, w7_reset_secs=86400
            ),
            Candidate(
                account=_acc("b"), h5_pct=10.0, w7_pct=10.0, h5_reset_secs=3600, w7_reset_secs=86400
            ),
        ]

    chosen: list[str] = []
    with (
        patch("claude_rotate.commands.run.probe_many", side_effect=two_equal),
        patch("claude_rotate.commands.run.reconcile_all"),
        patch("claude_rotate.commands.run.ensure_fresh", side_effect=lambda a, _p: a),
        patch(
            "claude_rotate.commands.run.exec_claude",
            side_effect=lambda acc, _p, _a, session_uuid=None: chosen.append(acc.name) or 0,
        ),
    ):
        execute(p, [])
        execute(p, [])

    assert set(chosen) == {"a", "b"}  # fanned out, not the same account twice


def test_run_fallback_tracks_session_when_no_probe_data(tmp_path) -> None:
    """The all-probes-failed fallback still reserves + injects a uuid."""
    from claude_rotate import sessions

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
        patch("claude_rotate.commands.run.ensure_fresh", side_effect=lambda a, _p: a),
        patch("claude_rotate.commands.run.exec_claude") as mock_exec,
    ):
        execute(p, [])

    recs = sessions.read_records(p)
    assert len(recs) == 1 and recs[0].account == "main"
    assert mock_exec.call_args.kwargs["session_uuid"] == recs[0].uuid


def test_run_tracking_disabled_writes_no_record(tmp_path) -> None:
    """session_tracking=false → pre-feature behaviour: no reservation, no uuid."""
    from claude_rotate import sessions
    from claude_rotate.settings import set_value

    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    Store(p).save({"main": _acc()})
    set_value(p, "session_tracking", "false")

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
        patch("claude_rotate.commands.run.reconcile_all"),
        patch("claude_rotate.commands.run.ensure_fresh", side_effect=lambda a, _p: a),
        patch("claude_rotate.commands.run.exec_claude") as mock_exec,
    ):
        execute(p, [])

    assert sessions.read_records(p) == []
    assert "session_uuid" not in mock_exec.call_args.kwargs


# --- account groups: allowed set, guards, silent launch ----------------------


def _grouped_run(
    monkeypatch,
    tmp_path: Path,
    accounts: dict[str, Account],
    groups: dict[str, Group],
    *,
    cwd: str,
    isolation: bool = True,
    quota: dict[str, float | None] | None = None,
    probe_errors: dict[str, str] | None = None,
) -> tuple[int, MagicMock, MagicMock]:
    """Run ``execute`` in ``tmp_path/cwd``; return (rc, probe_many mock, exec_claude mock).

    ``quota`` maps an account name to its 5h/7d percentage (default 10); ``None``
    makes that probe return no data. ``probe_errors`` overrides the probe error.
    """
    monkeypatch.delenv("CLAUDE_ROTATE_SESSION_ISOLATION", raising=False)
    p = _paths(tmp_path)
    p.config_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    Store(p).save(accounts)
    save_config(p, RotateConfig(session_isolation=isolation, groups=groups))
    (tmp_path / cwd).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path / cwd)
    quota = quota or {}
    probe_errors = probe_errors or {}

    def probe(accs: list[Account]) -> list[Candidate]:
        out = []
        for a in accs:
            pct = quota.get(a.name, 10.0)
            out.append(
                Candidate(
                    account=a,
                    h5_pct=pct,
                    w7_pct=pct,
                    h5_reset_secs=3600,
                    w7_reset_secs=86400,
                    probe_error=probe_errors.get(a.name, "" if pct is not None else "network"),
                )
            )
        return out

    with (
        patch("claude_rotate.commands.run.refresh_stale_accounts"),
        patch("claude_rotate.commands.run.refresh_stale_tokens", return_value=False),
        patch("claude_rotate.sync.reconcile_isolated"),
        patch("claude_rotate.commands.run.reconcile_all"),
        patch("claude_rotate.commands.run.probe_many", side_effect=probe) as probe_mock,
        patch("claude_rotate.commands.run.ensure_fresh", side_effect=lambda a, _p: a),
        patch("claude_rotate.commands.run.exec_claude") as exec_mock,
    ):
        rc = execute(p, [])
    return rc, probe_mock, exec_mock


def _bound(tmp_path: Path, *accounts: str) -> dict[str, Group]:
    return {"k": Group("k", accounts, (str(tmp_path.resolve() / "bound"),))}


def test_run_in_bound_dir_only_probes_and_picks_group(monkeypatch, tmp_path) -> None:
    accounts = {"a": _acc("a"), "b": _acc("b")}
    _, probe, exec_mock = _grouped_run(
        monkeypatch, tmp_path, accounts, _bound(tmp_path, "a"), cwd="bound/repo"
    )
    assert [x.name for x in probe.call_args[0][0]] == ["a"]
    assert exec_mock.call_args[0][0].name == "a"


def test_run_outside_bound_dir_never_picks_reserved(monkeypatch, tmp_path) -> None:
    accounts = {"a": _acc("a"), "b": _acc("b")}
    # a is fresher, but reserved for the bound directory
    _, probe, exec_mock = _grouped_run(
        monkeypatch,
        tmp_path,
        accounts,
        _bound(tmp_path, "a"),
        cwd="elsewhere",
        quota={"a": 1.0, "b": 50.0},
    )
    assert [x.name for x in probe.call_args[0][0]] == ["b"]
    assert exec_mock.call_args[0][0].name == "b"


def test_run_pinned_outside_group_is_ignored(monkeypatch, tmp_path) -> None:
    accounts = {"a": _acc("a"), "b": replace(_acc("b"), pinned=True)}
    _, _, exec_mock = _grouped_run(
        monkeypatch, tmp_path, accounts, _bound(tmp_path, "a"), cwd="bound"
    )
    assert exec_mock.call_args[0][0].name == "a"


def test_run_no_probe_data_fallback_stays_in_group(monkeypatch, tmp_path) -> None:
    accounts = {"a": _acc("a"), "b": replace(_acc("b"), pinned=True)}
    _, _, exec_mock = _grouped_run(
        monkeypatch,
        tmp_path,
        accounts,
        _bound(tmp_path, "a"),
        cwd="bound",
        quota={"a": None, "b": None},
    )
    assert exec_mock.call_args[0][0].name == "a"


def test_run_refuses_when_group_has_no_enabled_account(monkeypatch, tmp_path, capsys) -> None:
    accounts = {"a": replace(_acc("a"), disabled=True), "b": _acc("b")}
    rc, probe, exec_mock = _grouped_run(
        monkeypatch, tmp_path, accounts, _bound(tmp_path, "a"), cwd="bound"
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "k" in err and "group add" in err
    probe.assert_not_called()
    exec_mock.assert_not_called()


def test_run_refuses_when_all_reserved(monkeypatch, tmp_path, capsys) -> None:
    rc, _, exec_mock = _grouped_run(
        monkeypatch, tmp_path, {"a": _acc("a")}, _bound(tmp_path, "a"), cwd="elsewhere"
    )
    assert rc == 3
    assert "reserved" in capsys.readouterr().err
    exec_mock.assert_not_called()


def test_run_refuses_bound_groups_without_isolation(monkeypatch, tmp_path, capsys) -> None:
    rc, probe, exec_mock = _grouped_run(
        monkeypatch,
        tmp_path,
        {"a": _acc("a")},
        _bound(tmp_path, "a"),
        cwd="bound",
        isolation=False,
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "session_isolation" in err and "CLAUDE_ROTATE_SESSION_ISOLATION" in err
    probe.assert_not_called()
    exec_mock.assert_not_called()


def test_run_is_silent_on_success(monkeypatch, tmp_path, capsys) -> None:
    _grouped_run(monkeypatch, tmp_path, {"a": _acc("a"), "b": _acc("b")}, {}, cwd="anywhere")
    assert capsys.readouterr().err == ""


def test_run_prints_wait_line_when_group_exhausted(monkeypatch, tmp_path, capsys) -> None:
    accounts = {"a": _acc("a"), "b": _acc("b")}
    _, _, exec_mock = _grouped_run(
        monkeypatch,
        tmp_path,
        accounts,
        _bound(tmp_path, "a"),
        cwd="bound",
        quota={"a": 100.0},
    )
    err = capsys.readouterr().err
    assert "⏳" in err and "group k" in err
    assert exec_mock.call_args[0][0].name == "a"  # no fallback to other accounts


def test_run_warns_about_unauthorized_allowed_account(monkeypatch, tmp_path, capsys) -> None:
    accounts = {"a": replace(_acc("a"), email="a@example.com"), "b": _acc("b")}
    _grouped_run(
        monkeypatch,
        tmp_path,
        accounts,
        {},
        cwd="anywhere",
        quota={"a": None},
        probe_errors={"a": "unauthorized"},
    )
    err = capsys.readouterr().err
    assert "a: token invalid" in err and "claude-rotate login a@example.com a" in err
