"""`claude-rotate remove <name>`."""

from __future__ import annotations

import argparse
import sys

from claude_rotate.accounts import Store, resolve_name
from claude_rotate.config import Paths


def execute(paths: Paths, args: argparse.Namespace) -> int:
    store = Store(paths)
    accounts = store.load()
    name = resolve_name(accounts, args.name)
    if name is None:
        print(f"error: account {args.name!r} not found", file=sys.stderr)
        return 1

    a = accounts[name]
    print(
        f"\n  About to delete: {name} ({a.plan}, {a.email or '<no email>'})",
        file=sys.stderr,
    )
    if not args.yes:
        resp = input("  Proceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("  Aborted.", file=sys.stderr)
            return 1

    del accounts[name]
    store.save(accounts)

    # Delete associated caches
    usage = paths.usage_dir / f"{name}.json"
    if usage.exists():
        usage.unlink()

    print(f"  ✓ Removed {name}.\n", file=sys.stderr)
    return 0
