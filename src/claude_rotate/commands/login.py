"""`claude-rotate login` — thin glue between CLI args and login.py."""

from __future__ import annotations

import argparse
from pathlib import Path

from claude_rotate.config import Paths
from claude_rotate.login import (
    do_login_from_env,
    do_login_from_file,
    do_login_interactive,
)


def execute(paths: Paths, args: argparse.Namespace) -> int:
    if args.from_env:
        do_login_from_env(
            paths=paths,
            email=args.email,
            name=args.name,
            replace=args.replace,
        )
        return 0
    if args.token_file:
        do_login_from_file(
            paths=paths,
            email=args.email,
            name=args.name,
            token_path=Path(args.token_file),
            replace=args.replace,
        )
        return 0
    port = getattr(args, "port", 0) or 0
    do_login_interactive(
        paths=paths,
        email=args.email,
        name=args.name,
        claude_bin="",  # unused in PKCE flow
        replace=args.replace,
        port=port,
    )
    return 0
