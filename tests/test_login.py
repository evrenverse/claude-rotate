"""Tests for the login module.

The interactive path uses a real local HTTP callback server (no PTY, no
subprocess). Tests simulate Anthropic's OAuth redirect by making a GET request
to /callback directly, or by pre-seeding _CallbackHandler.received.
"""

from __future__ import annotations

import socketserver
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from claude_rotate.errors import AccountError, TokenFormatError
from claude_rotate.login import (
    _default_name_from_email,
    build_account,
    validate_token_format,
)

# ---------------------------------------------------------------------------
# validate_token_format
# ---------------------------------------------------------------------------


def test_accepts_valid_oat01_token() -> None:
    token = "sk-ant-oat01-" + "a" * 96
    validate_token_format(token)  # no raise


def test_rejects_empty_string() -> None:
    with pytest.raises(TokenFormatError, match="empty"):
        validate_token_format("")


def test_rejects_api_key_with_specific_message() -> None:
    token = "sk-ant-api03-" + "a" * 96
    with pytest.raises(TokenFormatError, match="API key"):
        validate_token_format(token)


def test_rejects_short_token() -> None:
    with pytest.raises(TokenFormatError, match="too short"):
        validate_token_format("sk-ant-oat01-short")


def test_rejects_unknown_prefix() -> None:
    with pytest.raises(TokenFormatError, match="prefix"):
        validate_token_format("foobar-something-long-" + "a" * 96)


def test_strips_surrounding_whitespace_before_validating() -> None:
    token = "  sk-ant-oat01-" + "a" * 96 + "\n"
    validate_token_format(token)  # must not raise


# ---------------------------------------------------------------------------
# build_account
# ---------------------------------------------------------------------------


def test_build_account_happy_path() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    a = build_account(
        name="main",
        token="sk-ant-oat01-" + "a" * 96,
        email="user@example.com",
        plan="max_20x",
        now=now,
    )
    assert a.name == "main"
    assert a.email == "user@example.com"
    assert a.plan == "max_20x"
    assert a.created_at == now
    assert a.subscription_expires_at is None
    assert a.label == "Max-20 main"
    assert a.pinned is False
    assert a.metadata_refreshed_at == now
    assert a.refresh_token is None


def test_build_account_max_5x_label() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    a = build_account(
        name="backup",
        token="sk-ant-oat01-" + "b" * 96,
        email="b@example.com",
        plan="max_5x",
        now=now,
    )
    assert a.label == "Max-5 backup"


def test_build_account_unknown_plan_uses_plan_as_label_prefix() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    a = build_account(
        name="x",
        token="sk-ant-oat01-" + "c" * 96,
        email="x@example.com",
        plan="enterprise",
        now=now,
    )
    assert a.plan == "enterprise"
    assert "x" in a.label


def test_build_account_with_refresh_token() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    a = build_account(
        name="main",
        token="sk-ant-oat01-" + "a" * 96,
        email="user@example.com",
        plan="max_20x",
        now=now,
        refresh_token="sk-ant-ort01-refresh",
    )
    assert a.refresh_token == "sk-ant-ort01-refresh"


def test_build_account_with_subscription_status() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    a = build_account(
        name="main",
        token="sk-ant-oat01-" + "a" * 96,
        email="user@example.com",
        plan="max_20x",
        now=now,
        subscription_status="active",
    )
    assert a.subscription_status == "active"


def test_build_account_subscription_status_defaults_none() -> None:
    now = datetime(2026, 4, 22, tzinfo=UTC)
    a = build_account(
        name="main",
        token="sk-ant-oat01-" + "a" * 96,
        email="user@example.com",
        plan="max_20x",
        now=now,
    )
    assert a.subscription_status is None


# ---------------------------------------------------------------------------
# _default_name_from_email
# ---------------------------------------------------------------------------


def test_default_name_from_email() -> None:
    assert _default_name_from_email("user@example.com") == "user"


def test_default_name_from_email_strips_special_chars() -> None:
    assert _default_name_from_email("john.doe+test@example.com") == "johndoetest"


def test_default_name_from_email_keeps_alnum_and_dash() -> None:
    assert _default_name_from_email("my-account@corp.io") == "my-account"


def test_default_name_from_email_empty_local_part_fallback() -> None:
    assert _default_name_from_email("@example.com") == "account"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path):  # type: ignore[no-untyped-def]
    from claude_rotate.config import Paths

    p = Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )
    p.config_dir.mkdir(parents=True)
    return p


def _make_token_pair(
    access_token: str = "at-" + "a" * 40,
    refresh_token: str = "rt-" + "r" * 40,
    expires_in: int = 28800,
) -> MagicMock:
    from claude_rotate.oauth import SCOPES, TokenPair

    now = datetime(2026, 4, 22, tzinfo=UTC)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scope=SCOPES,
        obtained_at=now,
    )


def _make_profile(
    email: str = "user@example.com",
    rate_limit_tier: str = "claude_max_20x",
) -> MagicMock:
    from claude_rotate.oauth import ProfileInfo

    return ProfileInfo(
        ok=True,
        email=email,
        rate_limit_tier=rate_limit_tier,
        subscription_status="active",
        subscription_created_at="2025-01-01T00:00:00Z",
    )


def _seed_callback(code: str = "mycode", state: str | None = None) -> None:
    """Pre-seed the callback handler received dict.

    ``state`` must match the PKCE verifier that ``do_login_interactive``
    sent in the authorize URL; ``_ImmediateEvent`` wires that up by
    reading the PKCE object the caller built just before starting the
    wait. If ``state`` is None we fall back to whatever
    ``_CurrentPKCE.verifier`` currently holds.
    """
    from claude_rotate.login import _CallbackHandler

    _CallbackHandler.received["code"] = code
    _CallbackHandler.received["state"] = state or _CurrentPKCE.verifier
    _CallbackHandler.received["error"] = None


class _CurrentPKCE:
    """Captures the PKCE verifier produced during the current test."""

    verifier: str = ""


def _patch_pkce() -> patch:  # type: ignore[type-arg]
    """Wrap ``generate_pkce`` so ``_CurrentPKCE.verifier`` tracks it."""
    from claude_rotate import oauth as _oauth

    real_generate = _oauth.generate_pkce

    def _capturing():  # type: ignore[no-untyped-def]
        pkce = real_generate()
        _CurrentPKCE.verifier = pkce.verifier
        return pkce

    return patch("claude_rotate.login.generate_pkce", side_effect=_capturing)


class _ImmediateEvent:
    """Duck-typed fake of ``threading.Event`` whose ``wait()`` pre-seeds the
    callback dict and returns ``True`` immediately.

    Used via ``patch('claude_rotate.login._new_callback_event')`` — so only
    the callback event is replaced, not the global ``threading.Event``
    class (which would also replace ``Thread._started`` internals and race
    on CI).
    """

    def __init__(self) -> None:
        self._is_set = False

    def wait(self, timeout: float | None = None) -> bool:
        _seed_callback()
        self._is_set = True
        return True

    def set(self) -> None:
        self._is_set = True

    def clear(self) -> None:
        self._is_set = False

    def is_set(self) -> bool:
        return self._is_set


def _patch_tcp_server() -> patch:  # type: ignore[type-arg]
    """Patch TCPServer.__init__ to bind on port 0 (avoids hardcoded ports in tests)."""
    original = socketserver.TCPServer.__init__

    def _patched(  # type: ignore[no-untyped-def]
        self_srv, server_address: tuple[str, int], RequestHandlerClass: type, **kw: object
    ) -> None:
        original(self_srv, ("127.0.0.1", 0), RequestHandlerClass)

    return patch.object(socketserver.TCPServer, "__init__", _patched)


# ---------------------------------------------------------------------------
# do_login_interactive — HTTP callback server flow
# ---------------------------------------------------------------------------


def test_do_login_interactive_saves_account(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Happy path: callback pre-seeded, token exchanged, profile fetched, account saved."""
    from claude_rotate.accounts import Store
    from claude_rotate.login import do_login_interactive

    paths = _make_paths(tmp_path)
    pair = _make_token_pair()
    profile = _make_profile(email="user@example.com")

    with (
        patch("claude_rotate.login.exchange_code", return_value=pair),
        patch("claude_rotate.login.fetch_profile", return_value=profile),
        patch("claude_rotate.login.webbrowser.open"),
        patch("claude_rotate.login._new_callback_event", side_effect=_ImmediateEvent),
        patch("claude_rotate.login._prompt_manual_expiry", return_value=None),
        _patch_pkce(),
        _patch_tcp_server(),
    ):
        acct = do_login_interactive(
            paths=paths,
            email="user@example.com",
            claude_bin="",
            name="main",
            skip_repeat_warning=True,
        )

    assert acct.name == "main"
    assert acct.email == "user@example.com"
    assert acct.plan == "max_20x"
    assert acct.refresh_token == pair.refresh_token
    assert acct.subscription_status == "active"
    assert Store(paths).load()["main"].runtime_token == pair.access_token
    assert Store(paths).load()["main"].subscription_status == "active"


def test_do_login_interactive_identity_mismatch_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Profile email != requested email → AccountError (Identity-Leak guard)."""
    from claude_rotate.login import do_login_interactive

    paths = _make_paths(tmp_path)
    pair = _make_token_pair()
    # Profile returns a DIFFERENT email
    profile = _make_profile(email="attacker@evil.com")

    with (
        patch("claude_rotate.login.exchange_code", return_value=pair),
        patch("claude_rotate.login.fetch_profile", return_value=profile),
        patch("claude_rotate.login.webbrowser.open"),
        patch("claude_rotate.login._new_callback_event", side_effect=_ImmediateEvent),
        patch("claude_rotate.login._prompt_manual_expiry", return_value=None),
        _patch_pkce(),
        _patch_tcp_server(),
        pytest.raises(AccountError, match="mismatch"),
    ):
        do_login_interactive(
            paths=paths,
            email="user@example.com",
            claude_bin="",
            name="main",
            skip_repeat_warning=True,
        )


def test_do_login_interactive_existing_account_without_replace(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """If name already exists and replace=False, raise AccountError."""
    from claude_rotate.accounts import Account, Store
    from claude_rotate.login import do_login_interactive

    paths = _make_paths(tmp_path)
    existing = Account(
        name="main",
        runtime_token="sk-ant-oat01-" + "x" * 96,
        label="main",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        plan="max_20x",
        email="user@example.com",
    )
    Store(paths).save({"main": existing})

    pair = _make_token_pair()
    profile = _make_profile(email="user@example.com")

    with (
        patch("claude_rotate.login.exchange_code", return_value=pair),
        patch("claude_rotate.login.fetch_profile", return_value=profile),
        patch("claude_rotate.login.webbrowser.open"),
        patch("claude_rotate.login._new_callback_event", side_effect=_ImmediateEvent),
        patch("claude_rotate.login._prompt_manual_expiry", return_value=None),
        _patch_pkce(),
        _patch_tcp_server(),
        pytest.raises(AccountError, match="already exists"),
    ):
        do_login_interactive(
            paths=paths,
            email="user@example.com",
            claude_bin="",
            name="main",
            replace=False,
            skip_repeat_warning=True,
        )


# ---------------------------------------------------------------------------
# do_login_from_env / do_login_from_file (non-interactive, CI path)
# ---------------------------------------------------------------------------


def test_do_login_from_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    paths = _make_paths(tmp_path)
    token = "sk-ant-oat01-" + "a" * 96
    monkeypatch.setenv("CLAUDE_ROTATE_TOKEN", token)

    from claude_rotate.login import do_login_from_env

    acct = do_login_from_env(paths=paths, email="ci@example.com", name="ci", replace=False)
    assert acct.name == "ci"
    assert acct.email == "ci@example.com"
    assert acct.plan == "unknown"
    assert acct.refresh_token is None


def test_do_login_from_env_derives_name_from_email(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    paths = _make_paths(tmp_path)
    token = "sk-ant-oat01-" + "a" * 96
    monkeypatch.setenv("CLAUDE_ROTATE_TOKEN", token)

    from claude_rotate.login import do_login_from_env

    acct = do_login_from_env(paths=paths, email="robot@corp.io", replace=False)
    assert acct.name == "robot"


def test_do_login_from_env_missing_var_raises(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("CLAUDE_ROTATE_TOKEN", raising=False)
    paths = _make_paths(tmp_path)
    with pytest.raises(AccountError, match="CLAUDE_ROTATE_TOKEN"):
        from claude_rotate.login import do_login_from_env

        do_login_from_env(paths=paths, email="ci@example.com", replace=False)


def test_do_login_from_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _make_paths(tmp_path)
    token_file = tmp_path / "token.txt"
    token_file.write_text("sk-ant-oat01-" + "a" * 96 + "\n")

    from claude_rotate.login import do_login_from_file

    acct = do_login_from_file(
        paths=paths, email="ci@example.com", name="ci", token_path=token_file, replace=False
    )
    assert acct.name == "ci"
    assert acct.email == "ci@example.com"
    assert acct.plan == "unknown"


def test_do_login_from_env_defaults_plan_to_unknown(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CI path has no /oauth/profile access, so plan is 'unknown' rather
    than inventing a fake value."""
    paths = _make_paths(tmp_path)
    token = "sk-ant-oat01-" + "a" * 96
    monkeypatch.setenv("CLAUDE_ROTATE_TOKEN", token)

    from claude_rotate.login import do_login_from_env

    acct = do_login_from_env(paths=paths, email="ci@example.com", name="ci", replace=False)
    assert acct.plan == "unknown"
    # label falls back to just the name when plan has no display mapping
    assert "ci" in acct.label
    assert acct.refresh_token is None
    assert acct.subscription_status is None
    assert acct.subscription_expires_at is None


def test_build_account_stamps_obtained_at() -> None:
    """OAuth login: both obtained_at timestamps are set to `now`."""
    from claude_rotate.login import build_account

    now = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
    acct = build_account(
        name="test",
        token="sk-ant-oat01-" + "a" * 100,
        email="test@example.com",
        plan="max_20x",
        now=now,
        refresh_token="sk-ant-ort01-" + "b" * 100,
    )
    assert acct.runtime_token_obtained_at == now
    assert acct.refresh_token_obtained_at == now


def test_build_account_ci_path_no_refresh_obtained_at() -> None:
    """CI path (refresh_token=None): runtime obtained_at set, refresh left None."""
    from claude_rotate.login import build_account

    now = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
    acct = build_account(
        name="test",
        token="sk-ant-oat01-" + "a" * 100,
        email="test@example.com",
        plan="unknown",
        now=now,
        refresh_token=None,
    )
    assert acct.runtime_token_obtained_at == now
    assert acct.refresh_token_obtained_at is None
