"""Full PKCE OAuth flow for Claude Code subscriptions.

Stdlib-only — no third-party deps beyond what ships with Python 3.11+.

Key endpoints (verified against claude-cli binary 2.1.117):
  Authorize : https://claude.com/cai/oauth/authorize
  Token     : https://platform.claude.com/v1/oauth/token
  Profile   : https://api.anthropic.com/api/oauth/profile

Client-ID  : 9d1c250a-e61b-44d9-88ed-5944d1962f5e  (Claude Code's public ID)
Scopes     : user:profile user:inference user:sessions:claude_code
             user:mcp_servers user:file_upload
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

from claude_rotate.config import ANTHROPIC_BETA, TOKEN_USER_AGENT, USER_AGENT
from claude_rotate.errors import ClaudeRotateError

# ---------------------------------------------------------------------------
# OAuth constants
# ---------------------------------------------------------------------------

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
SCOPES = "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PKCE:
    verifier: str
    challenge: str


def generate_pkce() -> PKCE:
    """Generate a fresh PKCE verifier + S256 challenge pair."""
    verifier = secrets.token_urlsafe(96)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return PKCE(verifier=verifier, challenge=challenge)


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------


def build_authorize_url(
    pkce: PKCE,
    *,
    redirect_uri: str,
    email: str | None = None,
) -> str:
    """Build the claude.com/cai/oauth/authorize URL.

    Mirrors claude-cli's pattern exactly:
    - state = verifier (the PKCE verifier doubles as CSRF state)
    - code_challenge_method = S256
    - response_type = code
    - code = true  (undocumented but required by Anthropic's endpoint)
    """
    params: dict[str, str] = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "state": pkce.verifier,
        "code": "true",
    }
    if email:
        params["login_hint"] = email
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# TokenPair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    obtained_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Token endpoint helper
# ---------------------------------------------------------------------------


def _post_token(body_dict: dict[str, str]) -> dict[str, object]:
    """POST to platform.claude.com/v1/oauth/token, return parsed JSON.

    On 4xx/5xx, reads the error body and raises ClaudeRotateError with full
    diagnostics so callers can see Anthropic's exact error reason.
    """
    body = json.dumps(body_dict).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # NOTE: this endpoint must NOT use the claude-code/... UA —
            # Anthropic rate-limits it specifically. See config.py comment.
            "User-Agent": TOKEN_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        # Anthropic's error responses sometimes echo the offending token
        # prefix back to us — redact any sk-ant-* sequence before surfacing.
        err_body = _redact_tokens(err_body)
        # Redact secrets in the echoed request body
        redacted = dict(body_dict)
        for k in ("code", "code_verifier", "refresh_token"):
            if redacted.get(k):
                v = str(redacted[k])
                redacted[k] = f"{v[:8]}…<{len(v)}c>"
        raise ClaudeRotateError(
            f"Token endpoint returned HTTP {exc.code}\n"
            f"  Request body (redacted): {json.dumps(redacted)}\n"
            f"  Response body: {err_body}"
        ) from exc


_TOKEN_RE = re.compile(r"sk-ant-(oat01|ort01|api01)-[\w\-]+")


def _redact_tokens(text: str) -> str:
    """Replace any ``sk-ant-…`` token substring with a redacted placeholder.

    Used on third-party response bodies before they reach logs or the
    user's terminal — Anthropic's error responses occasionally mirror the
    offending token back, which would leak it via copy-pasted tracebacks.
    """
    return _TOKEN_RE.sub(lambda m: f"sk-ant-{m.group(1)}-…<{len(m.group(0))}c>", text)


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


def exchange_code(code: str, verifier: str, redirect_uri: str) -> TokenPair:
    """Exchange an authorisation code for a TokenPair.

    The ``code`` parameter may arrive as ``<code>#<state>`` (some Anthropic
    callback variants include the state after a ``#``). We split on ``#`` and
    use the part before it as the actual code; the part after (or the verifier
    itself if no ``#``) is sent as ``state``.

    POST to platform.claude.com/v1/oauth/token with JSON body.
    User-Agent must be ``claude-code/2.1.117`` to bypass Cloudflare 1010.
    """
    if "#" in code:
        actual_code, state = code.split("#", 1)
    else:
        actual_code, state = code, verifier

    now = datetime.now(UTC)
    data = _post_token(
        {
            "grant_type": "authorization_code",
            "code": actual_code,
            "redirect_uri": redirect_uri,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
            "state": state,
        }
    )
    return TokenPair(
        access_token=str(data["access_token"]),
        refresh_token=str(data["refresh_token"]),
        expires_in=int(str(data.get("expires_in", 28800))),
        scope=str(data.get("scope", SCOPES)),
        obtained_at=now,
    )


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def refresh_access_token(refresh_token: str) -> TokenPair:
    """Obtain a fresh TokenPair using the refresh token.

    POST same endpoint with grant_type=refresh_token + scope.
    Returns a new TokenPair that includes the rotated refresh_token.
    """
    now = datetime.now(UTC)
    data = _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPES,
        }
    )
    return TokenPair(
        access_token=str(data["access_token"]),
        refresh_token=str(data["refresh_token"]),
        expires_in=int(str(data.get("expires_in", 28800))),
        scope=str(data.get("scope", SCOPES)),
        obtained_at=now,
    )


# ---------------------------------------------------------------------------
# Profile fetch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileInfo:
    ok: bool
    email: str | None = None
    rate_limit_tier: str | None = None
    subscription_status: str | None = None
    subscription_created_at: str | None = None
    error: str = ""

    @property
    def plan(self) -> str:
        """Map rate_limit_tier substring to plan name."""
        tier = self.rate_limit_tier or ""
        if "max_20x" in tier:
            return "max_20x"
        if "max_5x" in tier:
            return "max_5x"
        if "pro" in tier:
            return "pro"
        return "unknown"


def derive_subscription_expiry(
    *,
    subscription_status: str | None,
    subscription_created_at: str | None,
    now: datetime,
) -> datetime | None:
    """Return the next billing-anchor date for the subscription.

    - ``active``     → next billing cycle (subscription renews that day)
    - ``canceled`` / ``past_due`` → last period end before deactivation
    - anything else  → None

    The anchor day is extracted from ``subscription_created_at``; we find
    the earliest instance of that day-of-month that is strictly after
    ``now``. For the display layer, "active" and "canceled" with a
    concrete date both render as ``Nd`` — the distinction is shown via
    the urgency color (canceled-within-10d is yellow/red).
    """
    if not subscription_status:
        return None
    if subscription_status not in ("active", "canceled", "past_due"):
        return None
    if not subscription_created_at:
        return None
    import calendar

    created = datetime.fromisoformat(subscription_created_at.replace("Z", "+00:00"))
    anchor_day = created.day
    year, month = now.year, now.month
    for _ in range(2):
        max_day = calendar.monthrange(year, month)[1]
        day = min(anchor_day, max_day)
        period_end = created.replace(year=year, month=month, day=day)
        if period_end > now:
            return period_end
        month += 1
        if month > 12:
            month = 1
            year += 1
    return None


def fetch_profile(access_token: str) -> ProfileInfo:
    """GET api.anthropic.com/api/oauth/profile with Bearer + anthropic-beta header.

    Returns ``ProfileInfo(ok=False, error=...)`` on any 4xx/5xx rather than
    raising, so callers can treat failure gracefully.
    """
    req = urllib.request.Request(
        PROFILE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data: dict[str, object] = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return ProfileInfo(ok=False, error=f"HTTP {exc.code}: {exc.reason}")
    except Exception as exc:  # network errors, timeouts, etc.
        return ProfileInfo(ok=False, error=str(exc))

    account: dict[str, object] = data.get("account", {})  # type: ignore[assignment]
    org: dict[str, object] = data.get("organization", {})  # type: ignore[assignment]

    return ProfileInfo(
        ok=True,
        email=str(account.get("email", "")) or None,
        rate_limit_tier=str(org.get("rate_limit_tier", "")) or None,
        subscription_status=str(org.get("subscription_status", "")) or None,
        subscription_created_at=str(org.get("subscription_created_at", "")) or None,
    )
