"""Account dataclass and accounts.json schema handling.

The Store (load/save) lives in the same module but is added in Task 8. This
task only introduces the pure `Account` type and the JSON (de)serialization
helpers — no file I/O yet.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_rotate.config import Paths
from claude_rotate.errors import ConfigError, LockTimeoutError

SCHEMA_VERSION = 9
# Older schema versions that load without migration logic (new fields absent
# become None/default; saves always write the current SCHEMA_VERSION).
COMPATIBLE_SCHEMA_VERSIONS = {6, 7, 8, 9}


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    # Strip microseconds to keep JSON compact and diff-friendly
    return value.replace(microsecond=0).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Account:
    """A single rotated Anthropic subscription.

    `name` is the map key in accounts.json and is NOT serialised inside the
    per-account object (to avoid duplication).

    Schema v4 adds ``refresh_token`` (OAuth refresh token from the PKCE flow).
    Accounts installed via ``--from-env`` / ``--token-file`` (CI path) leave
    ``refresh_token=None`` — they cannot be automatically refreshed.

    ``runtime_token`` holds the OAuth *access* token (set as
    ``CLAUDE_CODE_OAUTH_TOKEN`` before execvpe).  The old v2/v3 distinction
    between ``access_token`` and ``runtime_token`` is collapsed: we use only
    ``runtime_token`` for the access token going forward.

    Schema v6: ``runtime_token_expires_at`` is removed. The access token
    has an 8h TTL; the refresh token has no documented TTL but is
    invalidated after ~2 weeks of non-use ("stale idle"). Displaying a
    fake "364d" was misleading — the stale-warning mechanism
    (``STALE_METADATA_WARN_DAYS``) is the honest replacement.

    Schema v7 adds ``subscription_expires_at_manual`` — a user-set override
    via ``claude-rotate set-expiry``. When set, it takes precedence over the
    API-derived ``subscription_expires_at`` for both display and selection,
    and ``metadata_refresh`` never touches it. Used when Anthropic's
    ``/oauth/profile`` fails to surface an upcoming cancellation (the
    canceled status only appears once the period actually ends, not when
    the user cancels on claude.ai).

    Schema v8 adds ``runtime_token_obtained_at`` and
    ``refresh_token_obtained_at``. The access token is pre-refreshed by
    ``refresh.ensure_fresh`` before each exec when older than the
    refresh threshold (see ``refresh_policy.py``); the obtained-at
    timestamps drive that decision. The refresh-token stamp also feeds
    ``doctor``'s stale-token warning independently of the metadata
    refresh cadence.

    Schema v9 adds ``disabled`` — a user-set manual exclusion via
    ``claude-rotate disable`` / ``enable``. A disabled account is removed
    from the selection pool entirely (never auto-picked, not even as a
    last-resort fallback), while still being probed and shown — rendered
    greyed-out with a "disabled" hint on the dashboard, list, and report.
    It is orthogonal to ``pinned`` (the two are mutually exclusive in
    practice: disabling clears a pin, pinning clears the disabled flag).
    """

    name: str
    runtime_token: str
    label: str
    created_at: datetime
    plan: str = "unknown"
    email: str | None = None
    subscription_expires_at: datetime | None = None
    pinned: bool = False
    metadata_refreshed_at: datetime | None = None
    refresh_token: str | None = None  # NEW in v4
    subscription_status: str | None = None  # NEW in v5
    subscription_expires_at_manual: datetime | None = None  # NEW in v7
    runtime_token_obtained_at: datetime | None = None  # NEW in v8
    refresh_token_obtained_at: datetime | None = None  # NEW in v8
    disabled: bool = False  # NEW in v9

    @property
    def effective_expires_at(self) -> datetime | None:
        """Return the manual override if set, else the API-derived value."""
        return self.subscription_expires_at_manual or self.subscription_expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_token": self.runtime_token,
            "refresh_token": self.refresh_token,
            "label": self.label,
            "created_at": _fmt_iso(self.created_at),
            "plan": self.plan,
            "email": self.email,
            "subscription_expires_at": _fmt_iso(self.subscription_expires_at),
            "subscription_expires_at_manual": _fmt_iso(self.subscription_expires_at_manual),
            "subscription_status": self.subscription_status,
            "pinned": self.pinned,
            "disabled": self.disabled,
            "metadata_refreshed_at": _fmt_iso(self.metadata_refreshed_at),
            "runtime_token_obtained_at": _fmt_iso(self.runtime_token_obtained_at),
            "refresh_token_obtained_at": _fmt_iso(self.refresh_token_obtained_at),
        }


def resolve_name(accounts: dict[str, Account], ident: str) -> str | None:
    """Map a user-supplied identifier to an account name.

    Accepts:
      - an exact handle (``work``)
      - an email address (``user@example.com``, case-insensitive)

    Returns the canonical handle, or ``None`` if no match is found. Raises
    ``ConfigError`` if ``ident`` looks like an email that matches more than
    one account — the caller must disambiguate via handle in that case.
    """
    if ident in accounts:
        return ident
    matches = [n for n, a in accounts.items() if a.email and a.email.lower() == ident.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigError(
            f"{ident!r} matches multiple accounts: {matches}. Use the handle instead."
        )
    return None


def account_from_dict(name: str, raw: dict[str, Any]) -> Account:
    return Account(
        name=name,
        runtime_token=raw["runtime_token"],
        label=raw.get("label") or f"{name}",
        created_at=_parse_iso(raw["created_at"]) or datetime.now(UTC),
        plan=raw.get("plan") or "unknown",
        email=raw.get("email"),
        subscription_expires_at=_parse_iso(raw.get("subscription_expires_at")),
        subscription_expires_at_manual=_parse_iso(raw.get("subscription_expires_at_manual")),
        subscription_status=raw.get("subscription_status"),
        pinned=bool(raw.get("pinned", False)),
        disabled=bool(raw.get("disabled", False)),
        metadata_refreshed_at=_parse_iso(raw.get("metadata_refreshed_at")),
        refresh_token=raw.get("refresh_token"),
        runtime_token_obtained_at=_parse_iso(raw.get("runtime_token_obtained_at")),
        refresh_token_obtained_at=_parse_iso(raw.get("refresh_token_obtained_at")),
    )


_LOCK_TIMEOUT_SECONDS = 10


class Store:
    """Load/save `accounts.json` atomically, with a flock on write paths."""

    def __init__(self, paths: Paths) -> None:
        self._paths = paths

    def load(self) -> dict[str, Account]:
        path = self._paths.accounts_file
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            backup = Path(f"{path}.corrupt-{int(time.time())}")
            path.rename(backup)
            raise ConfigError(f"{path} is malformed; backed up to {backup.name}: {exc}") from exc

        version = raw.get("version")
        if version not in COMPATIBLE_SCHEMA_VERSIONS:
            raise ConfigError(
                f"{path} has unknown schema version {version!r}; "
                f"expected one of {sorted(COMPATIBLE_SCHEMA_VERSIONS)}. Upgrade claude-rotate."
            )

        return {
            name: account_from_dict(name, body) for name, body in raw.get("accounts", {}).items()
        }

    def save(self, accounts: dict[str, Account]) -> None:
        """Serialize the whole map atomically under a flock."""
        with self._write_lock():
            self._save_unlocked(accounts)

    def _save_unlocked(self, accounts: dict[str, Account]) -> None:
        """``save`` body without acquiring the flock — caller already holds it."""
        path = self._paths.accounts_file
        path.parent.mkdir(parents=True, exist_ok=True)
        # Tokens live in this dir; enforce 0o700 idempotently. Without this,
        # the first save after a fresh install leaves the dir at default umask
        # (usually 0o755) and `doctor` rightly reports a warning.
        path.parent.chmod(0o700)

        payload = {
            "version": SCHEMA_VERSION,
            "accounts": {name: acct.to_dict() for name, acct in accounts.items()},
        }
        fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        tmp = Path(tmp_str)
        try:
            # chmod *before* writing any token bytes so the file is
            # never world/group-readable, not even for the microseconds
            # between the write and a post-write chmod.
            tmp.chmod(0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            tmp.replace(path)
        finally:
            if tmp.exists():
                tmp.unlink()

    @contextmanager
    def locked(self) -> Iterator[LockedStore]:
        """Hold the accounts.json flock across a read-modify-write block.

        ``save`` alone locks only the final write, so two processes (a cron
        tick and a ``run``) can each load the same rotating refresh token,
        both spend it, and trip Anthropic's refresh-token-reuse detection —
        which revokes the whole token family and forces a relogin.

        Inside ``with store.locked() as s:`` the exclusive flock is held for
        the entire block, so ``s.load()`` → refresh → ``s.save()`` runs as one
        critical section. Re-check ``should_refresh`` against the just-loaded
        token before spending it: another writer may have rotated it while we
        waited for the lock.
        """
        with self._write_lock():
            yield LockedStore(self)

    def _write_lock(self) -> _FlockGuard:
        return _FlockGuard(self._paths.lock_file)


class LockedStore:
    """A ``Store`` view whose ``load``/``save`` skip locking.

    Yielded by ``Store.locked()``; the flock is already held for the block,
    so re-locking inside (``save`` opens a second fd → ``flock`` is not
    recursive across fds in one process) would deadlock.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self) -> dict[str, Account]:
        return self._store.load()

    def save(self, accounts: dict[str, Account]) -> None:
        self._store._save_unlocked(accounts)


class _FlockGuard:
    """Non-blocking flock with a total wait ceiling."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    def __enter__(self) -> _FlockGuard:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.time() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() > deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise LockTimeoutError(
                        f"another claude-rotate writer held {self._lock_path} "
                        f"for >{_LOCK_TIMEOUT_SECONDS}s"
                    ) from None
                time.sleep(0.1)

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
