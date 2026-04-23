"""`claude-rotate pin <name>` / `unpin`."""

from __future__ import annotations

import sys
from dataclasses import replace

from claude_rotate.accounts import Store, resolve_name
from claude_rotate.config import Paths


def execute(paths: Paths, name: str | None, *, pinned: bool) -> int:
    store = Store(paths)
    accounts = store.load()
    if pinned:
        if name is None:
            print("error: pin requires a name", file=sys.stderr)
            return 1
        resolved = resolve_name(accounts, name)
        if resolved is None:
            print(f"error: account {name!r} not found", file=sys.stderr)
            return 1
        name = resolved
        accounts = {k: replace(v, pinned=(k == name)) for k, v in accounts.items()}
        store.save(accounts)
        print(f"  ✓ Pinned: {name}", file=sys.stderr)
    else:
        accounts = {k: replace(v, pinned=False) for k, v in accounts.items()}
        store.save(accounts)
        print("  ✓ Unpinned — rotation resumed", file=sys.stderr)
    return 0
