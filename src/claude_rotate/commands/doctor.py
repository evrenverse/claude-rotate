"""`claude-rotate doctor` — self-check."""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from claude_rotate.accounts import Store
from claude_rotate.config import STALE_METADATA_WARN_DAYS, Paths
from claude_rotate.errors import ClaudeBinaryError
from claude_rotate.exec import resolve_claude_binary
from claude_rotate.probe import fetch_usage

_LABEL_WIDTH = 22


def _print(ok: bool, label: str, value: str = "") -> None:
    mark = "✓" if ok else "✗"
    padded = label.ljust(_LABEL_WIDTH)
    if value:
        print(f"  {mark} {padded} {value}", file=sys.stderr)
    else:
        print(f"  {mark} {padded}", file=sys.stderr)


def _warn(label: str, value: str = "") -> None:
    padded = label.ljust(_LABEL_WIDTH)
    if value:
        print(f"  ⚠ {padded} {value}", file=sys.stderr)
    else:
        print(f"  ⚠ {padded}", file=sys.stderr)


def execute(paths: Paths) -> int:
    warnings = 0
    hard_errors = 0

    # 1. claude binary
    try:
        claude = resolve_claude_binary()
        _print(True, "claude binary", f"found at {claude}")
    except ClaudeBinaryError as exc:
        _print(False, "claude binary", str(exc))
        hard_errors += 1

    # 2. config dir
    if paths.config_dir.exists():
        mode = paths.config_dir.stat().st_mode & 0o777
        _print(mode == 0o700, "config dir", f"{paths.config_dir} (mode {mode:#o})")
        if mode != 0o700:
            warnings += 1
    else:
        _print(False, "config dir", f"{paths.config_dir}  (missing)")
        hard_errors += 1

    # 3. accounts.json
    accounts = Store(paths).load()
    _print(bool(accounts), "accounts.json", f"{len(accounts)} account(s)")

    # 4. network + tokens — fetch_usage for every account
    now = datetime.now(UTC)
    for name, acct in accounts.items():
        result = fetch_usage(acct.runtime_token)
        if result.ok:
            _print(True, f"token {name}", "valid")
        elif result.error == "unauthorized":
            _print(False, f"token {name}", "REJECTED: 401/403")
            hard_errors += 1
        elif result.error == "rate_limited":
            _print(True, f"token {name}", "valid, but account at quota limit")
            warnings += 1
        else:
            _print(True, f"token {name}", f"probe error: {result.error}")
            warnings += 1

        # 5. subscription status (if known)
        status = acct.subscription_status
        if status is not None:
            if status == "active":
                _print(True, f"subscription {name}", status)
            else:
                # Non-active: compute end date if available
                sub_end = acct.subscription_expires_at
                if sub_end is not None:
                    end_str = sub_end.strftime("%Y-%m-%d")
                    _warn(f"subscription {name}", f"{status} (ends {end_str})")
                else:
                    _warn(f"subscription {name}", status)
                warnings += 1

        # 6. stale metadata warning (OAuth accounts only)
        if acct.refresh_token is not None:
            last = acct.metadata_refreshed_at
            stale_days = None if last is None else (now - last).days
            if stale_days is None or stale_days > STALE_METADATA_WARN_DAYS:
                age_str = f"{stale_days}d ago" if stale_days is not None else "never"
                _warn(f"metadata {name}", f"stale {age_str} — token may be invalidated soon")
                warnings += 1
            else:
                _print(True, f"metadata {name}", f"fresh ({stale_days}d ago)")

        # 7. refresh_token staleness (OAuth accounts only)
        if acct.refresh_token is not None:
            rt_last = acct.refresh_token_obtained_at
            rt_stale_days = None if rt_last is None else (now - rt_last).days
            if rt_stale_days is None:
                _warn(f"refresh_token {name}", "age unknown — re-login to stamp")
                warnings += 1
            elif rt_stale_days > STALE_METADATA_WARN_DAYS:
                _warn(
                    f"refresh_token {name}",
                    f"stale {rt_stale_days}d ago — Anthropic may invalidate soon",
                )
                warnings += 1
            else:
                _print(True, f"refresh_token {name}", f"fresh ({rt_stale_days}d ago)")

    if hard_errors:
        return 2
    if warnings:
        return 1
    return 0
