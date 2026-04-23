"""Atomic read/write of ~/.claude/.credentials.json.

Claude Code's OAuth-PKCE login writes this file after a successful
`/login`. `claude-rotate run` writes it with the rotated account's
tokens so the child `claude` process boots with full session scope
instead of an inference-only env-var token.

Layout matches the shape Claude Code produces verbatim. We own the
write path; the child process owns updates during a live session
(refresh callbacks). `claude-rotate` reads it back on a cron cadence
(and before every `run`) to sync any rotation into accounts.json.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CredentialsPayload:
    access_token: str
    refresh_token: str | None
    expires_at_ms: int
    scopes: list[str]
    subscription_type: str
    rate_limit_tier: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "claudeAiOauth": {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": self.expires_at_ms,
                "scopes": self.scopes,
                "subscriptionType": self.subscription_type,
                "rateLimitTier": self.rate_limit_tier,
            }
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> CredentialsPayload:
        oauth_raw = raw.get("claudeAiOauth")
        if not isinstance(oauth_raw, dict):
            raise ValueError("credentials.json: missing 'claudeAiOauth' object")
        oauth: dict[str, object] = oauth_raw
        raw_expires = oauth.get("expiresAt", 0)
        raw_scopes = oauth.get("scopes") or []
        expires_at_ms = int(raw_expires) if isinstance(raw_expires, int | str) else 0
        scopes = [str(s) for s in raw_scopes] if isinstance(raw_scopes, list) else []
        return cls(
            access_token=str(oauth["accessToken"]),
            refresh_token=(
                None if oauth.get("refreshToken") is None else str(oauth["refreshToken"])
            ),
            expires_at_ms=expires_at_ms,
            scopes=scopes,
            subscription_type=str(oauth.get("subscriptionType") or "unknown"),
            rate_limit_tier=(
                None if oauth.get("rateLimitTier") is None else str(oauth["rateLimitTier"])
            ),
        )


def _home_claude_dir() -> Path:
    """Resolve ~/.claude honouring HOME so tests can redirect it."""
    return Path(os.environ.get("HOME", str(Path.home()))) / ".claude"


class CredentialsFile:
    """Owns the path and atomic IO for ~/.claude/.credentials.json."""

    def __init__(self) -> None:
        self._dir = _home_claude_dir()
        self.path = self._dir / ".credentials.json"

    def write(self, payload: CredentialsPayload) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._backup_existing()

        fd, tmp_str = tempfile.mkstemp(
            dir=str(self._dir),
            prefix=".credentials.json.tmp-",
        )
        tmp = Path(tmp_str)
        try:
            tmp.chmod(0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(payload.to_json(), f, indent=2)
                f.write("\n")
            tmp.replace(self.path)
        finally:
            if tmp.exists():
                tmp.unlink()

        # Opportunistic cleanup so backups don't accumulate forever.
        self.prune_backups(now=int(time.time()))

    def read(self) -> CredentialsPayload | None:
        if not self.path.exists():
            return None
        return CredentialsPayload.from_json(json.loads(self.path.read_text()))

    def _backup_existing(self) -> None:
        stamp = int(time.time())
        backup = self._dir / f".credentials.json.bak-{stamp}"
        backup.write_bytes(self.path.read_bytes())
        backup.chmod(0o600)

    def prune_backups(self, *, now: int, max_age_days: int = 7) -> None:
        threshold = now - max_age_days * 86400
        for backup in self._dir.glob(".credentials.json.bak-*"):
            try:
                ts = int(backup.name.rsplit("-", 1)[1])
            except ValueError:
                continue
            if ts < threshold:
                backup.unlink(missing_ok=True)


def write_credentials(payload: CredentialsPayload) -> None:
    CredentialsFile().write(payload)


def read_credentials() -> CredentialsPayload | None:
    return CredentialsFile().read()
