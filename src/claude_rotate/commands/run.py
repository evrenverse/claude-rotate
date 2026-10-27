"""`claude-rotate run …` — the default command path."""

from __future__ import annotations

import contextlib
import os
import sys
import time
import uuid as uuid_lib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from claude_rotate import sessions
from claude_rotate.accounts import Account, Store
from claude_rotate.config import (
    SESSION_ACTIVE_WINDOW_SECONDS,
    SESSION_IDLE_WEIGHT,
    Paths,
    file_lock,
)
from claude_rotate.dashboard import row_from_cache
from claude_rotate.exec import exec_claude
from claude_rotate.groups import (
    Group,
    allowed_accounts,
    has_bound_groups,
    matching_group,
    session_path,
)
from claude_rotate.metadata import refresh_stale_accounts
from claude_rotate.probe import probe_many, probe_result_of
from claude_rotate.refresh import ensure_fresh
from claude_rotate.selection import Candidate, is_usable, pick_best, selection_pool
from claude_rotate.settings import load_config
from claude_rotate.state_log import StateLog
from claude_rotate.sync import reconcile_all, refresh_stale_tokens
from claude_rotate.usage_cache import UsageCache


def _reserve_record(paths: Paths, account_name: str) -> str:
    """Write a session-registry record for this run and return its uuid.

    Reads our own pid + start_time — both survive execvpe, so the record points
    at the claude process we are about to become. Best-effort.
    """
    run_uuid = uuid_lib.uuid4().hex
    pid = os.getpid()
    now = time.time()
    sessions.write_record(
        paths,
        sessions.SessionRecord(
            uuid=run_uuid,
            account=account_name,
            pid=pid,
            start_time=sessions.process_start_time(pid) or 0.0,
            started_at=now,
            last_active=now,
        ),
    )
    return run_uuid


def _maybe_refresh(account: Account, paths: Paths, *, reconciled: bool) -> Account:
    """Pre-refresh this account's access token, unless the reconcile was skipped.

    ``ensure_fresh`` re-reads accounts.json under the lock, which protects it
    against *another process* rotating the token — but not against the running
    session rotating its own credentials file without that drift having been
    synced back. With an un-reconciled store the refresh token on disk may
    already be spent, and re-sending it trips Anthropic's reuse detection and
    revokes the whole family.

    So when the reconcile did not run we launch with whatever token is stored.
    That token may be expired, in which case claude refreshes it itself or
    prompts — a far cheaper failure than a revoked family, and the same
    degradation ``ensure_fresh`` already falls back to on any error.
    """
    return ensure_fresh(account, paths) if reconciled else account


def _rewrite_pinned_wait(
    wait_msg: str | None,
    pinned_names: set[str],
    best: Candidate,
    enabled_resolved: list[Candidate],
) -> str | None:
    if not (wait_msg and pinned_names):
        return wait_msg
    has_alternative = any(
        c.account.name not in pinned_names and is_usable(c) for c in enabled_resolved
    )
    in_idx = wait_msg.find(" in ")
    remainder = wait_msg[in_idx:] if in_idx != -1 else ""
    msg = f"pinned account {best.account.label!r} exhausted; available{remainder}"
    if has_alternative:
        msg += " — run `claude-rotate unpin` to rotate instead"
    return msg


def execute(paths: Paths, claude_args: list[str]) -> int:
    # Best-effort; never blocks or raises
    with contextlib.suppress(Exception):
        refresh_stale_accounts(paths)

    cfg = load_config(paths)

    # Pre-run reconcile: pull any drift the cron hasn't picked up yet. If the
    # lock is contended the reconcile is skipped — and so is the refresh below,
    # because accounts.json may still hold a token the running session already
    # spent, and re-sending it would trip reuse detection.
    reconciled = True
    try:
        from claude_rotate.sync import reconcile_isolated

        if cfg.session_isolation:
            reconcile_isolated(paths, now=datetime.now(UTC))
        else:
            reconcile_all(paths, now=datetime.now(UTC))
    except Exception:
        # Any failure — lock contention, a stat() error mid-iteration, an
        # unreadable store — leaves accounts.json possibly un-reconciled, so
        # treat them alike and skip every token refresh below.
        reconciled = False

    store = Store(paths)
    accounts = store.load()
    if not accounts:
        _print_no_accounts_message()
        return 3

    # Every account manually disabled → refuse before probing. Launching a
    # disabled account would defeat the whole point of `disable`.
    if all(a.disabled for a in accounts.values()):
        _print_all_disabled_message()
        return 3

    # Bound groups are only enforceable with per-account credentials: on the
    # shared ~/.claude/.credentials.json every launch would switch the running
    # sessions of the other directories to this account.
    if has_bound_groups(cfg.groups) and not cfg.session_isolation:
        _print_groups_need_isolation_message()
        return 3

    try:
        cwd = Path.cwd()
    except OSError:  # deleted cwd; "/" is bound to nothing
        cwd = Path("/")
    here = session_path(cwd)
    group = matching_group(cfg.groups, here)
    allowed = allowed_accounts(cfg.groups, accounts, here)
    if not any(not accounts[n].disabled for n in allowed):
        _print_nothing_allowed_message(group)
        return 3
    # Restrict once, here: every later path (probe, the no-probe-data fallback,
    # pin, selection) only ever sees accounts this directory may use.
    accounts = {n: a for n, a in accounts.items() if n in allowed}

    # Proactively refresh any account whose access token is stale BEFORE
    # we probe. Without this the probe hits Anthropic with a dead token,
    # the account is flagged 'relogin', and — even though a
    # later ensure_fresh in the chosen-account path would have recovered
    # — the child claude boots with whatever we could refresh (or not)
    # and may show the dreaded login prompt. Refreshing up-front keeps
    # every account usable.
    if reconciled:
        with contextlib.suppress(Exception):
            refreshed = refresh_stale_tokens(
                paths, now=datetime.now(UTC), isolated=cfg.session_isolation
            )
            if refreshed:
                # accounts.json was rewritten — reload so probe sees fresh tokens
                accounts = {n: a for n, a in store.load().items() if n in allowed}

    all_accounts = list(accounts.values())
    pinned_names = {a.name for a in all_accounts if a.pinned}
    disabled_names = {a.name for a in all_accounts if a.disabled}

    candidates = probe_many(all_accounts)
    cache = UsageCache(paths)

    # Fill in any candidates that failed live-probe with cached values
    resolved: list[Candidate] = []
    for c in candidates:
        if c.h5_pct is None and c.w7_pct is None:
            err = c.probe_error
            # Unauthorized → needs a re-login, skip from resolved. A disabled
            # account can never launch, so its dead token is not worth a warning.
            if err == "unauthorized":
                if c.account.name not in disabled_names:
                    print(
                        f"  ⚠ {c.account.name}: token invalid — run claude-rotate login "
                        f"{c.account.email or '<email>'} {c.account.name}",
                        file=sys.stderr,
                    )
                continue
            # Any other failure (429 without headers, 5xx, network) is a probe
            # failure rather than trustworthy quota data: fall back to cached
            # usage and keep the candidate in the selection pool.
            filled, _ = row_from_cache(c, cache, probe_error=err)
            if filled is None:
                continue
            c = filled
        else:
            cache.save(c.account.name, probe_result_of(c))
        resolved.append(c)

    # Disabled accounts are never selection candidates.
    enabled_resolved = [c for c in resolved if c.account.name not in disabled_names]

    group_name = group.name if group else None

    if not enabled_resolved:
        # No probe data for any *enabled* account → exec the first enabled
        # account with its cached token. Prefer a pinned-enabled one. An
        # enabled allowed account is guaranteed to exist (checked above).
        first = next(
            (a for a in accounts.values() if a.pinned and not a.disabled),
            next(a for a in accounts.values() if not a.disabled),
        )
        StateLog(paths).event("exec", chosen=first.name, reason="no_probe_data", group=group_name)
        fresh = _maybe_refresh(first, paths, reconciled=reconciled)
        if cfg.session_tracking:
            return exec_claude(
                fresh, paths, claude_args, session_uuid=_reserve_record(paths, first.name)
            )
        return exec_claude(fresh, paths, claude_args)

    # Shared with status.py. ``accounts`` is already restricted; ``allowed`` keeps
    # the two callers on the same rule.
    pool = selection_pool(resolved, accounts, allowed=allowed)

    if not cfg.session_tracking:
        # Tracking disabled → exact pre-feature behaviour.
        best, wait_msg = pick_best(pool)
        wait_msg = _rewrite_pinned_wait(wait_msg, pinned_names, best, enabled_resolved)
        _print_wait(wait_msg, group)
        StateLog(paths).event(
            "exec",
            chosen=best.account.name,
            h5_pct=best.h5_pct,
            w7_pct=best.w7_pct,
            wait=wait_msg,
            group=group_name,
        )
        fresh = _maybe_refresh(best.account, paths, reconciled=reconciled)
        return exec_claude(fresh, paths, claude_args)

    with file_lock(paths.sessions_lock):
        loads = sessions.count_load(
            paths, now=time.time(), active_window=float(SESSION_ACTIVE_WINDOW_SECONDS)
        )
        loaded_pool = [
            replace(
                c,
                session_load=loads[c.account.name].weighted(idle_weight=SESSION_IDLE_WEIGHT)
                if c.account.name in loads
                else 0.0,
            )
            for c in pool
        ]
        best, wait_msg = pick_best(loaded_pool)
        run_uuid = _reserve_record(paths, best.account.name)
        # If exec_claude fails below, this record is orphaned briefly but our
        # own pid dies with the failed run, so the next count_load reaps it.

    wait_msg = _rewrite_pinned_wait(wait_msg, pinned_names, best, enabled_resolved)
    _print_wait(wait_msg, group)
    StateLog(paths).event(
        "exec",
        chosen=best.account.name,
        h5_pct=best.h5_pct,
        w7_pct=best.w7_pct,
        wait=wait_msg,
        group=group_name,
    )
    fresh = _maybe_refresh(best.account, paths, reconciled=reconciled)
    return exec_claude(fresh, paths, claude_args, session_uuid=run_uuid)


def _print_wait(wait_msg: str | None, group: Group | None) -> None:
    if wait_msg:
        prefix = f"group {group.name}: " if group else ""
        print(f"  ⏳ {prefix}{wait_msg}", file=sys.stderr)


def _print_no_accounts_message() -> None:
    import glob
    import os

    home = os.environ.get("HOME", "")
    legacy = glob.glob(os.path.join(home, ".claude", ".credentials-*.json"))

    print("", file=sys.stderr)
    print("  No accounts configured.", file=sys.stderr)
    if legacy:
        print(
            "\n  I noticed legacy ~/.claude/.credentials-*.json files — those are\n"
            "  short-lived browser-OAuth tokens and not usable here. This tool uses\n"
            "  long-lived subscription tokens from `claude setup-token`.",
            file=sys.stderr,
        )
    print(
        "\n  Run: claude-rotate login <email> [name]\n",
        file=sys.stderr,
    )


def _print_all_disabled_message() -> None:
    print("", file=sys.stderr)
    print("  All accounts are manually disabled — nothing to rotate.", file=sys.stderr)
    print(
        "\n  Re-enable one with: claude-rotate enable <name>\n",
        file=sys.stderr,
    )


def _print_groups_need_isolation_message() -> None:
    print("", file=sys.stderr)
    print(
        "  Account groups are bound to directories, which needs session isolation —\n"
        "  without it every session shares one credentials file and a launch here\n"
        "  would switch all other running sessions to this directory's account.",
        file=sys.stderr,
    )
    print(
        "\n  Turn it on:  claude-rotate config set session_isolation true\n"
        "  (or unset CLAUDE_ROTATE_SESSION_ISOLATION=0 if the environment forces it off)\n",
        file=sys.stderr,
    )


def _print_nothing_allowed_message(group: Group | None) -> None:
    print("", file=sys.stderr)
    if group is not None:
        print(
            f"  Group {group.name!r} is bound to this directory but has no enabled account,"
            "\n  and every other enabled account is reserved for other directories.",
            file=sys.stderr,
        )
        print(
            f"\n  Add one:     claude-rotate group add {group.name} <account>"
            "\n  or enable:   claude-rotate enable <account>\n",
            file=sys.stderr,
        )
        return
    print(
        "  Every enabled account is reserved for other directories — none may run here.",
        file=sys.stderr,
    )
    print("\n  See the bindings: claude-rotate group list\n", file=sys.stderr)
