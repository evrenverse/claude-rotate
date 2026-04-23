"""Locate the real `claude` binary and exec it with credentials in place.

The previous implementation set ``CLAUDE_CODE_OAUTH_TOKEN`` in the child
env. Claude Code treats any env-var-supplied token as long-lived and
inference-only, which blocked Remote Control and other session-scope
features. Instead, we now write ``~/.claude/.credentials.json`` — the
same file Claude Code's own ``/login`` writes — and exec without the
env var set.

A user's shell may have `alias claude=claude-rotate run`. That alias is a
shell feature and not visible via `which`, so `shutil.which("claude")`
still returns the real binary — but if the user set up a *symlink* named
`claude` pointing back at this wrapper, we must refuse to recurse.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from claude_rotate.accounts import Account
from claude_rotate.config import Paths
from claude_rotate.credentials_file import CredentialsPayload, write_credentials
from claude_rotate.errors import ClaudeBinaryError
from claude_rotate.sync import CurrentSession, write_current_session

_TOKEN_TTL_MS = 8 * 3600 * 1000  # Anthropic's documented oat01 TTL

_FULL_SCOPES: list[str] = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]

_INFERENCE_ONLY: list[str] = ["user:inference"]

_PLAN_TO_SUBSCRIPTION: dict[str, tuple[str, str | None]] = {
    "max_20x": ("max", "default_claude_max_20x"),
    "max_5x": ("max", "default_claude_max_5x"),
    "pro": ("pro", "default_claude_pro"),
    "unknown": ("unknown", None),
}


def resolve_claude_binary() -> str:
    path = shutil.which("claude")
    if not path:
        raise ClaudeBinaryError(
            "The `claude` binary was not found on PATH. "
            "Install it: https://code.claude.com/docs/en/setup"
        )
    real = Path(path).resolve()
    if "claude-rotate" in real.name:
        raise ClaudeBinaryError(
            f"{path} points back at claude-rotate. Remove the symlink or reorder PATH."
        )
    return path


def build_credentials_payload(account: Account, *, now: datetime) -> CredentialsPayload:
    """Compose the JSON payload claude expects from an Account.

    CI-path accounts (no refresh_token) carry only user:inference because
    the setup-token flow genuinely grants only that scope. Widening the
    scope list here would be a lie that Claude Code's server would reject
    on the first privileged call.
    """
    is_oauth = account.refresh_token is not None
    scopes = _FULL_SCOPES if is_oauth else _INFERENCE_ONLY
    sub_type, rate_limit_tier = _PLAN_TO_SUBSCRIPTION.get(
        account.plan, _PLAN_TO_SUBSCRIPTION["unknown"]
    )
    now_ms = int(now.timestamp() * 1000)
    return CredentialsPayload(
        access_token=account.runtime_token,
        refresh_token=account.refresh_token,
        expires_at_ms=now_ms + _TOKEN_TTL_MS,
        scopes=scopes,
        subscription_type=sub_type,
        rate_limit_tier=rate_limit_tier,
    )


def exec_claude(account: Account, paths: Paths, args: list[str]) -> int:
    """Replace the current process with `claude`.

    Writes ~/.claude/.credentials.json with the account's tokens, records
    the session breadcrumb AFTER the credentials write (so cron never sees
    a mismatched pair), strips CLAUDE_CODE_OAUTH_TOKEN from the child env,
    and execvpe's.

    Returns only on error (os.execvpe never returns on success).
    """
    claude_bin = resolve_claude_binary()
    payload = build_credentials_payload(account, now=datetime.now(UTC))
    # Order is load-bearing: credentials first, breadcrumb second. Between
    # these two writes the cron would see stale session+new creds, which
    # is harmless (it checks account existence first). The reverse order
    # would let cron write the stale creds into the new account's slot.
    write_credentials(payload)
    write_current_session(paths, CurrentSession(account_name=account.name))

    env = dict(os.environ)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    # Ensure child inherits no stale CLAUDE_CONFIG_DIR pointing at a shadow home
    env.pop("CLAUDE_CONFIG_DIR", None)

    os.execvpe(claude_bin, [claude_bin, *args], env)
    return 1  # unreachable
