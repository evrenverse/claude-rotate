"""Login flow: full PKCE OAuth via local HTTP callback server.

Interactive path (`do_login_interactive`):
  1. Start an HTTP server on 127.0.0.1:<port> with a /callback handler
  2. Generate PKCE pair + build authorize URL
  3. Print the URL and open the browser (best-effort)
  4. Wait for Anthropic to call back with ?code=...
  5. Exchange the code for a TokenPair
  6. Fetch /oauth/profile to verify email + get plan
  7. Save the account

Non-interactive path (`do_login_from_env` / `do_login_from_file`):
  Accepts a raw sk-ant-oat01- token (CI path).  No browser, no profile check.
  Email, plan, and expiry are supplied by the caller.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import re
import socketserver
import sys
import threading
import webbrowser
from datetime import UTC, datetime, timedelta  # timedelta kept for parse_expiry
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths
from claude_rotate.errors import AccountError
from claude_rotate.oauth import (
    build_authorize_url,
    derive_subscription_expiry,
    exchange_code,
    fetch_profile,
    generate_pkce,
)

_OAT_PREFIX = "sk-ant-oat01-"

_VALID_PLANS = ("max_20x", "max_5x", "pro")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def validate_token_format(raw: str) -> str:
    """Return the cleaned token or raise TokenFormatError."""
    from claude_rotate.errors import TokenFormatError

    _API_PREFIX = "sk-ant-api03-"
    _MIN_LENGTH = 100
    _ALPHABET = re.compile(r"^[A-Za-z0-9_\-]+$")

    token = raw.strip()
    if not token:
        raise TokenFormatError("token is empty")
    if token.startswith(_API_PREFIX):
        raise TokenFormatError(
            "That looks like an API key (sk-ant-api03-), which bills per token "
            "against API credits. This tool expects a subscription OAuth token "
            "from `claude setup-token` (sk-ant-oat01-)."
        )
    if not token.startswith(_OAT_PREFIX):
        raise TokenFormatError(f"token has unexpected prefix; expected {_OAT_PREFIX!r}")
    if len(token) < _MIN_LENGTH:
        raise TokenFormatError(f"token too short ({len(token)} chars, expected ≥{_MIN_LENGTH})")
    suffix = token[len(_OAT_PREFIX) :]
    if not _ALPHABET.match(suffix):
        raise TokenFormatError("token contains characters outside [A-Za-z0-9_-]")
    return token


def build_account(
    *,
    name: str,
    token: str,
    email: str,
    plan: str,
    now: datetime,
    subscription_expires_at: datetime | None = None,
    subscription_expires_at_manual: datetime | None = None,
    subscription_status: str | None = None,
    refresh_token: str | None = None,
) -> Account:
    """Build an Account from supplied metadata."""
    plan_display_map = {"max_20x": "Max-20", "max_5x": "Max-5", "pro": "Pro"}
    return Account(
        name=name,
        runtime_token=token,
        refresh_token=refresh_token,
        label=f"{plan_display_map.get(plan, plan)} {name}".strip(),
        created_at=now,
        plan=plan,
        email=email,
        subscription_expires_at=subscription_expires_at,
        subscription_expires_at_manual=subscription_expires_at_manual,
        subscription_status=subscription_status,
        pinned=False,
        metadata_refreshed_at=now,
        runtime_token_obtained_at=now,
        refresh_token_obtained_at=now if refresh_token is not None else None,
    )


def parse_expiry(value: str | None) -> datetime | None:
    """Parse an expiry spec.

    Accepts:
      ``None`` or empty string → ``None``
      ``YYYY-MM-DD`` (absolute, EOD UTC)
      ``Nd`` (relative, N days from now)

    Raises AccountError on anything else.
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("d") and s[:-1].isdigit():
        return datetime.now(UTC) + timedelta(days=int(s[:-1]))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise AccountError(f"invalid expiry {value!r}; use YYYY-MM-DD, Nd, or leave empty") from e
    if dt.tzinfo is None:
        dt = dt.replace(hour=23, minute=59, second=59, tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _default_name_from_email(email: str) -> str:
    """'user@example.com' → 'user'. Strip special chars, keep alnum + dash."""
    local = email.split("@", 1)[0]
    cleaned = re.sub(r"[^A-Za-z0-9\-]", "", local)
    return cleaned or "account"


def _prompt_repeat_warning() -> None:
    """Print warning to stderr, wait for Enter. Raises on Ctrl-C."""
    _print("")
    _print("  WARNING: You have already logged in with this email before.")
    _print("  Logging in again will add a second account entry.")
    _print("  Press Enter to continue or Ctrl-C to abort.")
    try:
        input()
    except KeyboardInterrupt:
        raise AccountError("Login aborted by user.") from None


def _prompt_name(default: str) -> str:
    """Interactive prompt 'Save as account named [default]?' — Enter = default."""
    answer = input(f"  Save as account named [{default}]: ").strip()
    if not answer:
        return default
    if not re.match(r"^[A-Za-z0-9._\-]+$", answer):
        raise AccountError(
            f"Name {answer!r} contains characters outside [A-Za-z0-9._-]. "
            "Use a simple alphanumeric name."
        )
    return answer


def _prompt_manual_expiry() -> datetime | None:
    """Interactive prompt for a manual subscription-expiry override.

    Anthropic's /oauth/profile does not surface pending cancellations
    (status stays 'active' until the period actually ends). If the user
    has scheduled a cancel on claude.ai, we ask them to provide the date
    so the dashboard shows the real end date.

    Empty input → no override (default behaviour).
    Same format as set-expiry: YYYY-MM-DD, Nd, or blank.
    """
    raw = input("  Subscription ends on (YYYY-MM-DD or Nd, blank to skip): ").strip()
    if not raw:
        return None
    return parse_expiry(raw)


# ---------------------------------------------------------------------------
# HTTP callback server
# ---------------------------------------------------------------------------


def _new_callback_event() -> threading.Event:
    """Factory for the callback-wait event.

    Wrapping the ``threading.Event()`` constructor behind a factory gives
    tests a clean patch point — they can replace this with a fake event
    that returns immediately without monkey-patching the global
    ``threading.Event`` class (which would also affect
    ``Thread._started`` and cause stdlib-internal races on CI).
    """
    return threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles the single /callback GET from Anthropic's OAuth redirect."""

    received: ClassVar[dict[str, str | None]] = {}
    _event: ClassVar[threading.Event] = threading.Event()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        code_list: list[str] = qs.get("code") or []
        state_list: list[str] = qs.get("state") or []
        error_list: list[str] = qs.get("error") or []
        _CallbackHandler.received["code"] = code_list[0] if code_list else None
        _CallbackHandler.received["state"] = state_list[0] if state_list else None
        _CallbackHandler.received["error"] = error_list[0] if error_list else None
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<!doctype html><html><body style='font-family:system-ui;"
            b"text-align:center;padding:3rem;'>"
            b"<h1>\xe2\x9c\x93 Authorised</h1>"
            b"<p>You can close this tab and return to your terminal.</p>"
            b"</body></html>"
        )
        _CallbackHandler._event.set()

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silent


def _wait_for_callback(port: int = 0, timeout: int = 300) -> dict[str, str | None]:
    """Start a local HTTP server, wait for /callback, return the query params.

    Binds only to 127.0.0.1 (never 0.0.0.0).  If port=0, the OS picks a free
    port; the actual port is returned as ``result["_port"]``.
    """
    _CallbackHandler.received = {}
    _CallbackHandler._event = _new_callback_event()

    server = socketserver.TCPServer(("127.0.0.1", port), _CallbackHandler)
    server.allow_reuse_address = True
    actual_port = server.server_address[1]

    def _serve() -> None:
        server.serve_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    got_callback = _CallbackHandler._event.wait(timeout=timeout)
    server.shutdown()
    thread.join(timeout=5)

    result = dict(_CallbackHandler.received)
    result["_port"] = str(actual_port)
    if not got_callback:
        result["error"] = result.get("error") or "timeout"
    return result


# ---------------------------------------------------------------------------
# Public login API
# ---------------------------------------------------------------------------


def do_login_interactive(
    *,
    paths: Paths,
    email: str,
    claude_bin: str,  # kept for API back-compat; unused in PKCE flow
    name: str | None = None,
    replace: bool = False,
    skip_repeat_warning: bool = False,
    port: int = 0,
) -> Account:
    """Full PKCE OAuth via local callback server.

    1. Check existing accounts; warn if email already known
    2. Start HTTP server on 127.0.0.1:<port>
    3. Generate PKCE + build authorize_url with login_hint=email
    4. Print URL + open browser best-effort
    5. Wait for /callback with ?code=...
    6. exchange_code → TokenPair
    7. fetch_profile → validate email matches (Identity-Leak guard)
    8. Prompt for name if not supplied
    9. Save account

    ``claude_bin`` is accepted but unused; it is retained so callers that
    previously passed it do not need to be updated.
    """
    store = Store(paths)
    existing = store.load()

    # Repeat-login warning
    if not skip_repeat_warning:
        for acct in existing.values():
            if acct.email and acct.email.lower() == email.lower():
                _prompt_repeat_warning()
                break

    _print(f"\n  Logging in account for: {email}")

    # Start callback server (port=0 → OS chooses)
    _CallbackHandler.received = {}
    _CallbackHandler._event = _new_callback_event()
    server = socketserver.TCPServer(("127.0.0.1", port), _CallbackHandler)
    server.allow_reuse_address = True
    actual_port = server.server_address[1]
    # Anthropic's OAuth whitelist accepts ``http://localhost:PORT/callback``
    # (hostname ``localhost``, not the IP ``127.0.0.1``). Using the IP form
    # triggers ``invalid_grant: Invalid 'redirect_uri' in request.`` at token
    # exchange, even though the browser redirect itself resolves fine.
    redirect_uri = f"http://localhost:{actual_port}/callback"

    pkce = generate_pkce()
    authorize_url = build_authorize_url(pkce, redirect_uri=redirect_uri, email=email)

    _print("\n  Opening browser for authorisation…")
    _print(f"\n  If the browser does not open, visit:\n  {authorize_url}\n")

    with contextlib.suppress(Exception):
        webbrowser.open(authorize_url)

    # Run server in background thread, wait for callback
    def _serve() -> None:
        server.serve_forever()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    _print("  Waiting for authorisation callback… (Ctrl-C to abort)")
    try:
        got = _CallbackHandler._event.wait(timeout=300)
    except KeyboardInterrupt:
        server.shutdown()
        thread.join(timeout=5)
        raise AccountError("Login aborted by user.") from None

    server.shutdown()
    thread.join(timeout=5)

    if not got:
        raise AccountError("Timed out waiting for OAuth callback (5 minutes).")

    params = _CallbackHandler.received
    if params.get("error"):
        raise AccountError(f"OAuth authorisation error: {params['error']}")

    code = params.get("code")
    if not code:
        raise AccountError("No authorisation code received in callback.")

    # CSRF guard: the ``state`` parameter must echo back the PKCE verifier
    # we sent in the authorize URL. A mismatch means the callback came from
    # a different flow (hostile page or stale browser tab) — abort before
    # exchanging the code, which PKCE alone would still let us do.
    received_state = params.get("state")
    if received_state != pkce.verifier:
        raise AccountError(
            "OAuth state mismatch — callback did not originate from the "
            "authorize URL this session opened. Aborting."
        )

    # Exchange code for tokens
    _print("  Exchanging authorisation code for tokens…")
    try:
        pair = exchange_code(code, pkce.verifier, redirect_uri)
    except Exception as exc:
        raise AccountError(f"Token exchange failed: {exc}") from exc

    # Fetch profile — validates that the token belongs to the expected email
    _print("  Fetching account profile…")
    profile = fetch_profile(pair.access_token)
    if not profile.ok:
        raise AccountError(f"Profile fetch failed: {profile.error}")

    # Identity-Leak guard
    if profile.email and email and profile.email.lower() != email.lower():
        raise AccountError(
            f"Identity mismatch: you requested login for {email!r} but the token "
            f"belongs to {profile.email!r}. Use --replace after logging out of the "
            "other account, or start with a fresh browser session."
        )

    # Use profile email if user's email was empty-ish
    resolved_email = profile.email or email

    # Prompt for account name if not provided
    if name is None:
        default = _default_name_from_email(resolved_email)
        name = _prompt_name(default)

    # Prompt for manual expiry override. Anthropic's profile endpoint does
    # not surface pending cancellations — if the user has scheduled one on
    # claude.ai they can enter that date here and it will drive the
    # dashboard display + selection instead of the API-derived anchor.
    sub_expires_manual = _prompt_manual_expiry()

    # Duplicate-name guard
    current = store.load()
    if name in current and not replace:
        raise AccountError(f"account {name!r} already exists. Use --replace to overwrite.")

    now = datetime.now(UTC)
    plan = profile.plan if profile.plan != "unknown" else "max_20x"

    sub_status = profile.subscription_status
    sub_expires = derive_subscription_expiry(
        subscription_status=sub_status,
        subscription_created_at=profile.subscription_created_at,
        now=now,
    )

    account = build_account(
        name=name,
        token=pair.access_token,
        email=resolved_email,
        plan=plan,
        now=now,
        refresh_token=pair.refresh_token,
        subscription_status=sub_status,
        subscription_expires_at=sub_expires,
        subscription_expires_at_manual=sub_expires_manual,
    )

    current[name] = account
    store.save(current)

    _print(f"\n  Saved to {paths.accounts_file}")
    _print(f"\n  Account {name!r} is ready (plan: {plan}).\n")
    return account


def do_login_from_env(
    *,
    paths: Paths,
    email: str,
    name: str | None = None,
    replace: bool = False,
) -> Account:
    """Non-interactive — token from CLAUDE_ROTATE_TOKEN env (CI path).

    CI tokens come from ``claude setup-token`` which only carries
    ``user:inference`` scope, so we can't auto-populate plan/subscription.
    Plan is stored as ``"unknown"`` and subscription fields stay empty.
    User can edit ``accounts.json`` directly, or re-run ``login`` in
    interactive mode to get full metadata.
    """
    raw = os.environ.get("CLAUDE_ROTATE_TOKEN")
    if not raw:
        raise AccountError(
            "CLAUDE_ROTATE_TOKEN is not set. Either set it or use --token-file <path>."
        )
    return _save_account(paths=paths, email=email, name=name, raw_token=raw, replace=replace)


def do_login_from_file(
    *,
    paths: Paths,
    email: str,
    name: str | None = None,
    token_path: Path,
    replace: bool = False,
) -> Account:
    """Non-interactive — token read from file (CI path)."""
    try:
        raw = token_path.read_text()
    except OSError as e:
        raise AccountError(f"cannot read token file {token_path}: {e}") from e
    return _save_account(paths=paths, email=email, name=name, raw_token=raw, replace=replace)


def _save_account(
    *,
    paths: Paths,
    email: str,
    name: str | None,
    raw_token: str,
    replace: bool,
) -> Account:
    """Validate token format and save with honest defaults (CI path).

    CI tokens come from ``claude setup-token``, which only carries
    ``user:inference`` — we cannot call ``/oauth/profile`` to auto-populate
    plan/subscription. Rather than invent a plan, we store ``"unknown"``
    and leave subscription metadata empty. Interactive login fills these
    in properly; this codepath is for non-browser environments only.
    """
    token = validate_token_format(raw_token)
    if name is None:
        name = _default_name_from_email(email)

    store = Store(paths)
    if name in store.load() and not replace:
        raise AccountError(f"account {name!r} already exists. Pass --replace to overwrite.")

    now = datetime.now(UTC)
    account = build_account(
        name=name,
        token=token,
        email=email,
        plan="unknown",
        now=now,
        subscription_expires_at=None,
        subscription_status=None,
        refresh_token=None,  # CI path: no OAuth refresh token
    )

    current = store.load()
    current[name] = account
    store.save(current)

    _print(f"  Saved to {paths.accounts_file}")
    _print(f"\n  Account {name!r} is ready.\n")
    return account


def _print(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
