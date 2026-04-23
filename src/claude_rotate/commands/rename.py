"""`claude-rotate rename <old> <new>`."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from claude_rotate.accounts import Store, resolve_name
from claude_rotate.config import Paths


def execute(paths: Paths, args: argparse.Namespace) -> int:
    store = Store(paths)
    accounts = store.load()
    old = resolve_name(accounts, args.old)
    if old is None:
        print(f"error: account {args.old!r} not found", file=sys.stderr)
        return 1
    if args.new in accounts:
        print(f"error: account {args.new!r} already exists", file=sys.stderr)
        return 1

    acct = accounts.pop(old)
    accounts[args.new] = replace(acct, name=args.new)
    store.save(accounts)

    # Move usage cache if it exists
    old_cache = paths.usage_dir / f"{old}.json"
    new_cache = paths.usage_dir / f"{args.new}.json"
    if old_cache.exists():
        old_cache.rename(new_cache)

    print(f"  ✓ Renamed {old} → {args.new}", file=sys.stderr)
    return 0
