"""`claude-rotate disable <name>` / `enable <name>`.

A disabled account is manually excluded from rotation: ``pick_best`` never
sees it, not even as a last-resort fallback when every other account is
exhausted. It is still probed and shown (greyed-out with a "disabled" hint)
so the user keeps the full picture. Unlike ``pin``, disabling is not
exclusive — any number of accounts may be disabled at once.

Disabling a pinned account also clears the pin: a disabled account must
never be the forced pick.
"""

from __future__ import annotations

import sys
from dataclasses import replace

from claude_rotate.accounts import Store, resolve_name
from claude_rotate.config import Paths


def execute(paths: Paths, name: str, *, disabled: bool) -> int:
    store = Store(paths)
    accounts = store.load()
    resolved = resolve_name(accounts, name)
    if resolved is None:
        print(f"error: account {name!r} not found", file=sys.stderr)
        return 1
    name = resolved

    target = accounts[name]
    updates: dict[str, object] = {"disabled": disabled}
    if disabled and target.pinned:
        # A disabled account can never be the forced pick — drop the pin.
        updates["pinned"] = False
    accounts[name] = replace(target, **updates)  # type: ignore[arg-type]
    store.save(accounts)

    if disabled:
        print(
            f"  ✓ Disabled: {name} — excluded from rotation until `claude-rotate enable {name}`",
            file=sys.stderr,
        )
    else:
        print(f"  ✓ Enabled: {name} — back in rotation", file=sys.stderr)
    return 0
