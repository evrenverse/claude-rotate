"""Argparse dispatch and top-level error handling."""

from __future__ import annotations

import argparse
import sys

from claude_rotate import __version__
from claude_rotate.config import paths
from claude_rotate.errors import ClaudeRotateError

ROTATOR_COMMANDS = {
    "run",
    "login",
    "list",
    "remove",
    "rename",
    "pin",
    "unpin",
    "status",
    "doctor",
    "set-expiry",
    "cleanup",
    "sync-credentials",
    "install-sync",
}
ROTATOR_ROOT_FLAGS = {"--version", "-V", "--help", "-h"}

_TOP_DESCRIPTION = """\
Quota-aware account rotator for Claude Code subscriptions."""

_TOP_EPILOG = """\
Commands:
  run          Pick best account, exec claude  (default if no subcommand)
  login        Add or re-login an account
  list         List configured accounts
  remove       Delete an account
  rename       Rename an account
  pin          Pin an account so it is always used
  unpin        Resume rotation
  status       Show the quota dashboard
  doctor       Self-check: binary, config, network
  set-expiry   Override subscription end date manually
  cleanup      Delete all claude-rotate state (accounts, cache, logs)
  sync-credentials   Reconcile ~/.claude/.credentials.json → accounts.json (cron entry point)
  install-sync       Install a crontab entry running sync-credentials every 2 minutes

Options:
  -h, --help           Show this help
  -V, --version        Print version

Examples:
  claude-rotate login user@example.com
  claude-rotate status

With `alias claude='claude-rotate run'` in your shell, `claude <args>`
forwards everything to the real claude binary, and `claude-rotate
<command>` stays reserved for rotator-only operations.

Run `claude-rotate <command> --help` for help on a specific command.\
"""

_LOGIN_DESCRIPTION = """\
Add or re-login an account.

Opens the browser for an OAuth PKCE flow. After authorisation the tool
captures the callback on a local port, exchanges the code for long-lived
tokens, and saves the account to ~/.config/claude-rotate/accounts.json.\
"""

_LOGIN_EPILOG = """\
Arguments:
  email                Anthropic account email (pre-fills the login form)
  name                 Handle to save the account under (default: derived from email)

Options:
  --replace            Re-login an existing account (overwrite)
  --from-env           Non-interactive: read token from CLAUDE_ROTATE_TOKEN
  --token-file <path>  Non-interactive: read token from file
  -h, --help           Show this help

Example:
  claude-rotate login user@example.com main\
"""

_REMOVE_DESCRIPTION = """\
Delete an account from the store.\
"""

_REMOVE_EPILOG = """\
Arguments:
  name                 Account handle to delete

Options:
  --yes                Skip the confirmation prompt
  -h, --help           Show this help\
"""

_RENAME_DESCRIPTION = """\
Rename an account handle.\
"""

_RENAME_EPILOG = """\
Arguments:
  old                  Current account handle
  new                  New account handle

Options:
  -h, --help           Show this help\
"""

_PIN_DESCRIPTION = """\
Pin an account so it is always chosen, bypassing rotation.\
"""

_PIN_EPILOG = """\
Arguments:
  name                 Account handle to pin

Options:
  -h, --help           Show this help\
"""

_STATUS_DESCRIPTION = """\
Show the quota dashboard for all configured accounts.\
"""

_STATUS_EPILOG = """\
Options:
  --json               Output JSON instead of the coloured table
  -h, --help           Show this help\
"""

_DOCTOR_DESCRIPTION = """\
Self-check: verify the claude binary, config directory, accounts, and tokens.\
"""

_DOCTOR_EPILOG = """\
Options:
  -h, --help           Show this help\
"""

_SET_EXPIRY_DESCRIPTION = """\
Override the subscription end date manually.

Anthropic's profile API does not surface pending cancellations — the
subscription stays 'active' until the period actually ends. If you have
scheduled a cancellation on claude.ai, set the real end date here so the
dashboard and rotation heuristic use it instead of the next billing anchor.\
"""

_SET_EXPIRY_EPILOG = """\
Arguments:
  name                 Account handle
  value                YYYY-MM-DD, Nd (N days from now), or "" to clear

Options:
  -h, --help           Show this help

Examples:
  claude-rotate set-expiry work 2026-04-24
  claude-rotate set-expiry work 5d
  claude-rotate set-expiry work ""\
"""

_CLEANUP_DESCRIPTION = """\
Delete all claude-rotate state from disk.

Removes ``config_dir`` (accounts.json + lock), ``cache_dir`` (usage cache),
and ``state_dir`` (log.jsonl). Does not touch ~/.claude — anything claude
itself manages (plugins, memory, history, credentials.json) is out of
scope. Destructive; confirmation required unless ``--yes``.\
"""

_CLEANUP_EPILOG = """\
Options:
  --yes                Skip the confirmation prompt
  -h, --help           Show this help\
"""

# Sentinel used to suppress auto-generated argument entries from help output.
_SUPPRESS = argparse.SUPPRESS


def _preprocess_argv(argv: list[str]) -> list[str]:
    """Transform user argv into what argparse expects.

    Strategy:
    - [] → ['run']  (no args means launch claude interactively)
    - first arg is 'run' → ['run', '--', …rest]  so ``claude --help``
      (= ``claude-rotate run --help`` via the alias) passes cleanly through
      argparse as a REMAINDER instead of being swallowed by the parent
      parser's ``-h/--help`` action.
    - first arg is any other rotator command → pass through unchanged
    - first arg is a rotator root flag → pass through unchanged
    - anything else → prepend 'run --' so REMAINDER catches everything
    """
    if not argv:
        return ["run"]
    first = argv[0]
    if first == "run":
        return ["run", "--", *argv[1:]]
    if first in ROTATOR_COMMANDS:
        return argv
    if first in ROTATOR_ROOT_FLAGS:
        return argv
    return ["run", "--", *argv]


def _build_parser() -> argparse.ArgumentParser:
    fmt = argparse.RawDescriptionHelpFormatter
    p = argparse.ArgumentParser(
        prog="claude-rotate",
        usage="claude-rotate [options] <command> [<args>]",
        description=_TOP_DESCRIPTION,
        epilog=_TOP_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    # Suppress auto-generated entries; our epilog carries the full Commands/Options/Examples.
    p.add_argument("--version", "-V", action="version", version=__version__, help=_SUPPRESS)
    p.add_argument("-h", "--help", action="help", help=_SUPPRESS)
    sub = p.add_subparsers(dest="command")
    # Hide the auto-generated "positional arguments: {run,login,...}" block.
    if p._subparsers is not None:  # always set after add_subparsers, but mypy needs a guard
        for _action in p._subparsers._group_actions:
            _action.help = _SUPPRESS

    # run — pure passthrough. ``add_help=False`` so ``claude --help``
    # (= ``claude-rotate run --help`` via the alias) flows through to the
    # real claude instead of being swallowed by argparse's auto-help.
    sp_run = sub.add_parser(
        "run",
        help="Pick best account, exec claude",
        formatter_class=fmt,
        add_help=False,
    )
    sp_run.add_argument("args", nargs=argparse.REMAINDER)

    # login
    sp_login = sub.add_parser(
        "login",
        help="Add or re-login an account",
        usage="claude-rotate login [options] <email> [<name>]",
        description=_LOGIN_DESCRIPTION,
        epilog=_LOGIN_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    sp_login.add_argument("email", help=_SUPPRESS)
    sp_login.add_argument("name", nargs="?", default=None, help=_SUPPRESS)
    sp_login.add_argument("--replace", action="store_true", help=_SUPPRESS)
    sp_login.add_argument("--from-env", action="store_true", help=_SUPPRESS)
    sp_login.add_argument("--token-file", type=str, default=None, help=_SUPPRESS)
    sp_login.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    # list — no extra options
    sub.add_parser(
        "list",
        help="List configured accounts",
        formatter_class=fmt,
        add_help=True,
    )

    # remove
    sp_remove = sub.add_parser(
        "remove",
        help="Delete an account",
        usage="claude-rotate remove [options] <name>",
        description=_REMOVE_DESCRIPTION,
        epilog=_REMOVE_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    sp_remove.add_argument("name", help=_SUPPRESS)
    sp_remove.add_argument("--yes", action="store_true", help=_SUPPRESS)
    sp_remove.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    # rename
    sp_rename = sub.add_parser(
        "rename",
        help="Rename an account",
        usage="claude-rotate rename <old> <new>",
        description=_RENAME_DESCRIPTION,
        epilog=_RENAME_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    sp_rename.add_argument("old", help=_SUPPRESS)
    sp_rename.add_argument("new", help=_SUPPRESS)
    sp_rename.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    # pin
    sp_pin = sub.add_parser(
        "pin",
        help="Pin an account so it is always used",
        usage="claude-rotate pin <name>",
        description=_PIN_DESCRIPTION,
        epilog=_PIN_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    sp_pin.add_argument("name", help=_SUPPRESS)
    sp_pin.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    # unpin
    sub.add_parser(
        "unpin",
        help="Resume rotation",
        formatter_class=fmt,
        add_help=True,
    )

    # status
    sp_status = sub.add_parser(
        "status",
        help="Show the quota dashboard",
        usage="claude-rotate status [options]",
        description=_STATUS_DESCRIPTION,
        epilog=_STATUS_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    sp_status.add_argument("--json", action="store_true", help=_SUPPRESS)
    sp_status.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    # doctor
    sp_doctor = sub.add_parser(
        "doctor",
        help="Self-check: binary, config, network",
        usage="claude-rotate doctor",
        description=_DOCTOR_DESCRIPTION,
        epilog=_DOCTOR_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    sp_doctor.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    # set-expiry
    sp_set_expiry = sub.add_parser(
        "set-expiry",
        help="Override subscription end date manually",
        usage='claude-rotate set-expiry <name> <YYYY-MM-DD | Nd | "">',
        description=_SET_EXPIRY_DESCRIPTION,
        epilog=_SET_EXPIRY_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    sp_set_expiry.add_argument("name", help=_SUPPRESS)
    sp_set_expiry.add_argument("value", help=_SUPPRESS)
    sp_set_expiry.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    # cleanup
    sp_cleanup = sub.add_parser(
        "cleanup",
        help="Delete all claude-rotate state",
        usage="claude-rotate cleanup [--yes]",
        description=_CLEANUP_DESCRIPTION,
        epilog=_CLEANUP_EPILOG,
        formatter_class=fmt,
        add_help=False,
    )
    sp_cleanup.add_argument("--yes", action="store_true", help=_SUPPRESS)
    sp_cleanup.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    # sync-credentials (no options)
    sub.add_parser(
        "sync-credentials",
        help="Reconcile credentials file back to accounts.json",
        formatter_class=fmt,
        add_help=True,
    )

    # install-sync
    sp_install = sub.add_parser(
        "install-sync",
        help="Install crontab entry for periodic sync",
        formatter_class=fmt,
        add_help=False,
    )
    sp_install.add_argument("--uninstall", action="store_true", help=_SUPPRESS)
    sp_install.add_argument("-h", "--help", action="help", help=_SUPPRESS)

    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _preprocess_argv(argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0

    # Bare `claude-rotate …` (no subcommand) acts as `run`
    if args.command is None:
        args.command = "run"
        args.args = []  # argparse.REMAINDER would have left it; be explicit

    p = paths()
    try:
        if args.command == "run":
            from claude_rotate.commands import run

            # Strip a leading '--' sentinel inserted by _preprocess_argv
            claude_args = args.args
            if claude_args and claude_args[0] == "--":
                claude_args = claude_args[1:]
            return run.execute(p, claude_args)
        if args.command == "login":
            from claude_rotate.commands import login

            return login.execute(p, args)
        if args.command == "list":
            from claude_rotate.commands import list_cmd

            return list_cmd.execute(p)
        if args.command == "remove":
            from claude_rotate.commands import remove

            return remove.execute(p, args)
        if args.command == "rename":
            from claude_rotate.commands import rename

            return rename.execute(p, args)
        if args.command == "pin":
            from claude_rotate.commands import pin

            return pin.execute(p, args.name, pinned=True)
        if args.command == "unpin":
            from claude_rotate.commands import pin

            return pin.execute(p, name=None, pinned=False)
        if args.command == "status":
            from claude_rotate.commands import status

            return status.execute(p, as_json=args.json)
        if args.command == "doctor":
            from claude_rotate.commands import doctor

            return doctor.execute(p)
        if args.command == "set-expiry":
            from claude_rotate.commands import set_expiry

            return set_expiry.execute(p, args.name, args.value)
        if args.command == "cleanup":
            from claude_rotate.commands import cleanup

            return cleanup.execute(p, assume_yes=args.yes)
        if args.command == "sync-credentials":
            from claude_rotate.commands import sync_credentials

            return sync_credentials.execute(p)
        if args.command == "install-sync":
            from claude_rotate.commands import install_sync

            return install_sync.execute(p, uninstall=args.uninstall)
    except ClaudeRotateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unknown subcommand: {args.command}")
    return 2
