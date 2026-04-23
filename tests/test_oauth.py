"""Tests for the PKCE OAuth module (no real network calls)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from claude_rotate.oauth import (
    AUTHORIZE_URL,
    CLIENT_ID,
    PKCE,
    PROFILE_URL,
    SCOPES,
    TOKEN_URL,
    ProfileInfo,
    TokenPair,
    _redact_tokens,
    build_authorize_url,
    derive_subscription_expiry,
    exchange_code,
    fetch_profile,
    generate_pkce,
    refresh_access_token,
)

# ---------------------------------------------------------------------------
# generate_pkce
# ---------------------------------------------------------------------------


def test_generate_pkce_returns_pkce() -> None:
    p = generate_pkce()
    assert isinstance(p, PKCE)
    assert len(p.verifier) > 40
    assert len(p.challenge) > 20


def test_generate_pkce_different_each_call() -> None:
    a = generate_pkce()
    b = generate_pkce()
    assert a.verifier != b.verifier
    assert a.challenge != b.challenge


def test_generate_pkce_s256_relationship() -> None:
    """challenge = base64url(sha256(verifier)) without padding."""
    import base64
    import hashlib

    p = generate_pkce()
    digest = hashlib.sha256(p.verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert p.challenge == expected


# ---------------------------------------------------------------------------
# build_authorize_url
# ---------------------------------------------------------------------------


def test_build_authorize_url_contains_correct_base() -> None:
    pkce = PKCE(verifier="ver123", challenge="chal456")
    url = build_authorize_url(pkce, redirect_uri="http://127.0.0.1:12345/callback")
    assert url.startswith(AUTHORIZE_URL)


def test_build_authorize_url_contains_5_scopes() -> None:
    pkce = PKCE(verifier="ver", challenge="chal")
    url = build_authorize_url(pkce, redirect_uri="http://127.0.0.1:9999/callback")
    import urllib.parse

    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    scopes = qs["scope"][0].split(" ")
    assert "user:profile" in scopes
    assert "user:inference" in scopes
    assert "user:sessions:claude_code" in scopes
    assert "user:mcp_servers" in scopes
    assert "user:file_upload" in scopes
    assert len(scopes) == 5


def test_build_authorize_url_contains_client_id() -> None:
    pkce = PKCE(verifier="v", challenge="c")
    url = build_authorize_url(pkce, redirect_uri="http://127.0.0.1:1/callback")
    assert CLIENT_ID in url


def test_build_authorize_url_state_equals_verifier() -> None:
    import urllib.parse

    pkce = PKCE(verifier="my-verifier-value", challenge="chal")
    url = build_authorize_url(pkce, redirect_uri="http://127.0.0.1:1/callback")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["state"][0] == "my-verifier-value"


def test_build_authorize_url_contains_redirect_uri_with_port() -> None:
    import urllib.parse

    pkce = PKCE(verifier="v", challenge="c")
    redirect = "http://127.0.0.1:54321/callback"
    url = build_authorize_url(pkce, redirect_uri=redirect)
    assert urllib.parse.quote(redirect, safe="") in url or redirect in urllib.parse.unquote(url)


def test_build_authorize_url_login_hint_included() -> None:
    import urllib.parse

    pkce = PKCE(verifier="v", challenge="c")
    url = build_authorize_url(
        pkce, redirect_uri="http://127.0.0.1:1/callback", email="user@example.com"
    )
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["login_hint"][0] == "user@example.com"


def test_build_authorize_url_no_login_hint_when_email_none() -> None:
    import urllib.parse

    pkce = PKCE(verifier="v", challenge="c")
    url = build_authorize_url(pkce, redirect_uri="http://127.0.0.1:1/callback", email=None)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert "login_hint" not in qs


def test_build_authorize_url_code_true_param() -> None:
    import urllib.parse

    pkce = PKCE(verifier="v", challenge="c")
    url = build_authorize_url(pkce, redirect_uri="http://127.0.0.1:1/callback")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs.get("code", [None])[0] == "true"


# ---------------------------------------------------------------------------
# exchange_code
# ---------------------------------------------------------------------------


def _make_token_response(
    access_token: str = "at-tok",
    refresh_token: str = "rt-tok",
    expires_in: int = 28800,
) -> MagicMock:
    """Create a mock HTTP response for a token endpoint call."""
    body = json.dumps(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
            "scope": SCOPES,
        }
    ).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_exchange_code_posts_to_token_url() -> None:
    with patch("urllib.request.urlopen", return_value=_make_token_response()) as mock_open:
        exchange_code("mycode", "myverifier", "http://127.0.0.1:9/callback")
    req = mock_open.call_args[0][0]
    assert req.full_url == TOKEN_URL
    assert req.method == "POST"


def test_exchange_code_returns_token_pair() -> None:
    with patch("urllib.request.urlopen", return_value=_make_token_response("at", "rt")):
        pair = exchange_code("code", "verifier", "http://127.0.0.1:1/callback")
    assert isinstance(pair, TokenPair)
    assert pair.access_token == "at"
    assert pair.refresh_token == "rt"


def test_exchange_code_splits_hash_in_code() -> None:
    """code#state format: only the part before # is sent as code."""
    captured: list[bytes] = []

    def _mock_open(req, timeout=None):  # type: ignore[no-untyped-def]
        captured.append(req.data)
        return _make_token_response()

    with patch("urllib.request.urlopen", side_effect=_mock_open):
        exchange_code("mycode#mystate", "ver", "http://127.0.0.1:1/callback")

    body = json.loads(captured[0])
    assert body["code"] == "mycode"
    assert body["state"] == "mystate"


def test_exchange_code_sends_token_user_agent_not_claude_code() -> None:
    """The token endpoint must NOT receive ``claude-code/…``: Anthropic
    rate-limits that UA specifically on this endpoint. See config.py."""
    from claude_rotate.config import TOKEN_USER_AGENT

    with patch("urllib.request.urlopen", return_value=_make_token_response()) as mock_open:
        exchange_code("c", "v", "http://127.0.0.1:1/callback")
    req = mock_open.call_args[0][0]
    ua = req.headers.get("User-agent", "")
    assert ua == TOKEN_USER_AGENT
    assert "claude-code" not in ua


def test_exchange_code_sends_json_content_type() -> None:
    with patch("urllib.request.urlopen", return_value=_make_token_response()) as mock_open:
        exchange_code("c", "v", "http://127.0.0.1:1/callback")
    req = mock_open.call_args[0][0]
    assert req.headers.get("Content-type") == "application/json"


# ---------------------------------------------------------------------------
# refresh_access_token
# ---------------------------------------------------------------------------


def test_refresh_access_token_posts_to_token_url() -> None:
    with patch("urllib.request.urlopen", return_value=_make_token_response()) as mock_open:
        refresh_access_token("rt")
    req = mock_open.call_args[0][0]
    assert req.full_url == TOKEN_URL
    assert req.method == "POST"


def test_refresh_access_token_sends_refresh_grant() -> None:
    captured: list[bytes] = []

    def _mock_open(req, timeout=None):  # type: ignore[no-untyped-def]
        captured.append(req.data)
        return _make_token_response("new-at", "new-rt")

    with patch("urllib.request.urlopen", side_effect=_mock_open):
        pair = refresh_access_token("old-rt")

    body = json.loads(captured[0])
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "old-rt"
    assert body["client_id"] == CLIENT_ID
    assert pair.access_token == "new-at"
    assert pair.refresh_token == "new-rt"


# ---------------------------------------------------------------------------
# _redact_tokens — defence-in-depth for Anthropic error bodies
# ---------------------------------------------------------------------------


def test_redact_oauth_access_token() -> None:
    raw = 'error: "sk-ant-oat01-ABCDEFghijklmn-Xyz123" not valid'
    out = _redact_tokens(raw)
    assert "sk-ant-oat01-ABCDEF" not in out
    assert "sk-ant-oat01-…" in out


def test_redact_refresh_and_api_tokens() -> None:
    raw = "rt=sk-ant-ort01-FOObarbaz key=sk-ant-api01-ANOTHERone"
    out = _redact_tokens(raw)
    assert "FOObarbaz" not in out
    assert "ANOTHERone" not in out


def test_redact_idempotent_on_clean_strings() -> None:
    assert _redact_tokens("no secrets here") == "no secrets here"


# ---------------------------------------------------------------------------
# ProfileInfo.plan
# ---------------------------------------------------------------------------


def test_profile_info_plan_max_20x() -> None:
    p = ProfileInfo(ok=True, rate_limit_tier="claude_max_20x")
    assert p.plan == "max_20x"


def test_profile_info_plan_max_5x() -> None:
    p = ProfileInfo(ok=True, rate_limit_tier="claude_max_5x")
    assert p.plan == "max_5x"


def test_profile_info_plan_pro() -> None:
    p = ProfileInfo(ok=True, rate_limit_tier="claude_pro_v1")
    assert p.plan == "pro"


def test_profile_info_plan_unknown() -> None:
    p = ProfileInfo(ok=True, rate_limit_tier="something_else")
    assert p.plan == "unknown"


def test_profile_info_plan_none_rate_limit_tier() -> None:
    p = ProfileInfo(ok=True, rate_limit_tier=None)
    assert p.plan == "unknown"


# ---------------------------------------------------------------------------
# fetch_profile
# ---------------------------------------------------------------------------


def _make_profile_response(
    email: str = "user@example.com",
    rate_limit_tier: str = "claude_max_20x",
    subscription_status: str = "active",
) -> MagicMock:
    body = json.dumps(
        {
            "account": {"email": email},
            "organization": {
                "rate_limit_tier": rate_limit_tier,
                "subscription_status": subscription_status,
                "subscription_created_at": "2025-01-01T00:00:00Z",
            },
        }
    ).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_fetch_profile_returns_profile_info() -> None:
    with patch("urllib.request.urlopen", return_value=_make_profile_response()):
        info = fetch_profile("my-access-token")
    assert info.ok is True
    assert info.email == "user@example.com"
    assert info.rate_limit_tier == "claude_max_20x"
    assert info.subscription_status == "active"


def test_fetch_profile_sends_bearer_token() -> None:
    with patch("urllib.request.urlopen", return_value=_make_profile_response()) as mock_open:
        fetch_profile("test-token")
    req = mock_open.call_args[0][0]
    assert req.headers.get("Authorization") == "Bearer test-token"


def test_fetch_profile_sends_anthropic_beta() -> None:
    with patch("urllib.request.urlopen", return_value=_make_profile_response()) as mock_open:
        fetch_profile("tok")
    req = mock_open.call_args[0][0]
    assert "oauth" in req.headers.get("Anthropic-beta", "")


def test_fetch_profile_posts_to_profile_url() -> None:
    with patch("urllib.request.urlopen", return_value=_make_profile_response()) as mock_open:
        fetch_profile("tok")
    req = mock_open.call_args[0][0]
    assert req.full_url == PROFILE_URL


def test_fetch_profile_returns_ok_false_on_http_error() -> None:
    import urllib.error

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(PROFILE_URL, 401, "Unauthorized", {}, None),
    ):
        info = fetch_profile("bad-token")
    assert info.ok is False
    assert "401" in info.error


def test_fetch_profile_returns_ok_false_on_network_error() -> None:
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        info = fetch_profile("tok")
    assert info.ok is False
    assert info.error != ""


# ---------------------------------------------------------------------------
# derive_subscription_expiry
# ---------------------------------------------------------------------------


def test_derive_expiry_active_returns_next_billing_anchor() -> None:
    """Active subscriptions also get a next-billing-anchor date — the
    dashboard shows it as ``Nd`` to answer "wie viele Tage noch bis zur
    nächsten Abbuchung"."""
    now = datetime(2026, 4, 22, tzinfo=UTC)
    result = derive_subscription_expiry(
        subscription_status="active",
        subscription_created_at="2025-11-15T15:46:39Z",
        now=now,
    )
    # Billing anchor day=15; next 15th after 2026-04-22 is 2026-05-15
    assert result is not None
    assert result.year == 2026
    assert result.month == 5
    assert result.day == 15


def test_derive_expiry_none_status_returns_none() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    result = derive_subscription_expiry(
        subscription_status=None,
        subscription_created_at="2025-11-15T15:46:39Z",
        now=now,
    )
    assert result is None


def test_derive_expiry_missing_created_at_returns_none() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    result = derive_subscription_expiry(
        subscription_status="canceled",
        subscription_created_at=None,
        now=now,
    )
    assert result is None


def test_derive_expiry_canceled_returns_next_billing_anchor() -> None:
    """canceled subscription created on the 15th → next period end is the 15th."""
    now = datetime(2026, 4, 22, tzinfo=UTC)
    result = derive_subscription_expiry(
        subscription_status="canceled",
        subscription_created_at="2025-11-15T15:46:39Z",
        now=now,
    )
    assert result is not None
    assert result.day == 15
    # Should be next month since today is the 22nd (past the 15th)
    assert result.month == 5
    assert result.year == 2026


def test_derive_expiry_anchor_day_in_future_this_month() -> None:
    """If anchor day hasn't passed yet this month, period end is this month."""
    now = datetime(2026, 4, 10, tzinfo=UTC)
    result = derive_subscription_expiry(
        subscription_status="canceled",
        subscription_created_at="2025-11-15T00:00:00Z",
        now=now,
    )
    assert result is not None
    assert result.day == 15
    assert result.month == 4
    assert result.year == 2026


def test_derive_expiry_past_due_status_works() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    result = derive_subscription_expiry(
        subscription_status="past_due",
        subscription_created_at="2025-11-15T00:00:00Z",
        now=now,
    )
    assert result is not None
    assert result.month == 5
