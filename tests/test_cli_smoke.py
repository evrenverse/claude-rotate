# tests/test_cli_smoke.py
from __future__ import annotations

from unittest.mock import patch

from claude_rotate.cli import main


def test_version_flag_prints_version(capsys) -> None:
    rc = main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "0.1.0"


def test_short_version_flag_prints_version(capsys) -> None:
    rc = main(["-V"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "0.1.0"


def test_help_flag_exits_zero(capsys) -> None:
    # ``claude-rotate --help`` prints the rotator help; exits 0.
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-rotate" in out


def test_short_help_flag_exits_zero(capsys) -> None:
    rc = main(["-h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-rotate" in out


def test_run_help_forwards_to_claude(monkeypatch, tmp_path) -> None:
    # ``claude --help`` via alias (== ``claude-rotate run --help``) must
    # pass ``--help`` through to the real claude binary, not be absorbed
    # by the rotator's top-level help action.
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    with patch("claude_rotate.commands.run.execute") as mock_run:
        mock_run.return_value = 0
        rc = main(["run", "--help"])
    assert rc == 0
    mock_run.assert_called_once()
    _paths, claude_args = mock_run.call_args.args
    assert "--help" in claude_args


def test_bare_flag_forwards_to_run(monkeypatch, tmp_path) -> None:
    # -p "hi" should be dispatched as if main(["run", "--", "-p", "hi"])
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    with patch("claude_rotate.commands.run.execute") as mock_run:
        mock_run.return_value = 0
        rc = main(["-p", "hi"])
    assert rc == 0
    mock_run.assert_called_once()
    _paths, claude_args = mock_run.call_args.args
    assert "-p" in claude_args
    assert "hi" in claude_args


def test_empty_argv_dispatches_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    with patch("claude_rotate.commands.run.execute") as mock_run:
        mock_run.return_value = 0
        rc = main([])
    assert rc == 0
    mock_run.assert_called_once()
    _paths, claude_args = mock_run.call_args.args
    assert claude_args == []


def test_arbitrary_string_first_forwards_to_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    with patch("claude_rotate.commands.run.execute") as mock_run:
        mock_run.return_value = 0
        rc = main(["hello"])
    assert rc == 0
    mock_run.assert_called_once()
    _paths, claude_args = mock_run.call_args.args
    assert "hello" in claude_args


def test_subcommand_help_still_works(capsys) -> None:
    # login --help should show the rotator's login subcommand help and exit 0
    rc = main(["login", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "login" in out


def test_list_with_no_accounts_prints_hint(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    rc = main(["list"])
    # Exits 0 even when there's nothing to show
    assert rc == 0
    out = capsys.readouterr().err
    assert "login" in out.lower()


def test_status_json_flag(tmp_path, monkeypatch, capsys) -> None:
    # status --json is a rotator subcommand flag, should still work
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    rc = main(["status", "--json"])
    # No accounts → non-zero but should not crash with unrecognised flag
    assert isinstance(rc, int)


def test_passthrough_multiple_flags(monkeypatch, tmp_path) -> None:
    # claude-rotate -p "test" --model sonnet-4-6 → run with those args
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    with patch("claude_rotate.commands.run.execute") as mock_run:
        mock_run.return_value = 0
        rc = main(["-p", "test", "--model", "sonnet-4-6"])
    assert rc == 0
    _paths, claude_args = mock_run.call_args.args
    assert "-p" in claude_args
    assert "test" in claude_args
    assert "--model" in claude_args
    assert "sonnet-4-6" in claude_args


def test_dangerously_skip_permissions_passthrough(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLAUDE_ROTATE_DIR", str(tmp_path))
    with patch("claude_rotate.commands.run.execute") as mock_run:
        mock_run.return_value = 0
        rc = main(["--dangerously-skip-permissions", "-c"])
    assert rc == 0
    _paths, claude_args = mock_run.call_args.args
    assert "--dangerously-skip-permissions" in claude_args
    assert "-c" in claude_args
