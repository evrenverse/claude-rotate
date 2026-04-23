# tests/test_exec.py
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from claude_rotate.accounts import Account
from claude_rotate.errors import ClaudeBinaryError
from claude_rotate.exec import (
    build_credentials_payload,
    exec_claude,
    resolve_claude_binary,
)


def _acc(**overrides) -> Account:
    now = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
    fields = {
        "name": "main",
        "runtime_token": "sk-ant-oat01-" + "a" * 100,
        "label": "Max-20 main",
        "created_at": now,
        "plan": "max_20x",
        "refresh_token": "sk-ant-ort01-" + "b" * 100,
        "runtime_token_obtained_at": now,
        "refresh_token_obtained_at": now,
    }
    fields.update(overrides)
    return Account(**fields)


def test_build_credentials_payload_oauth_path() -> None:
    acct = _acc()
    now = datetime(2026, 4, 23, 10, 0, tzinfo=UTC)
    payload = build_credentials_payload(acct, now=now)

    assert payload.access_token == acct.runtime_token
    assert payload.refresh_token == acct.refresh_token
    assert "user:sessions:claude_code" in payload.scopes
    assert "user:mcp_servers" in payload.scopes
    assert payload.subscription_type == "max"
    # expiresAt ~= now + 8h
    eight_h_ms = 8 * 3600 * 1000
    now_ms = int(now.timestamp() * 1000)
    assert abs(payload.expires_at_ms - (now_ms + eight_h_ms)) < 1000


def test_build_credentials_payload_ci_path() -> None:
    """CI accounts have only user:inference — don't fabricate wider scopes."""
    acct = _acc(refresh_token=None, refresh_token_obtained_at=None, plan="unknown")
    payload = build_credentials_payload(acct, now=datetime.now(UTC))

    assert payload.refresh_token is None
    assert payload.scopes == ["user:inference"]
    assert payload.subscription_type == "unknown"


def test_exec_claude_writes_credentials_and_unsets_env(tmp_path, monkeypatch) -> None:
    from claude_rotate.config import Paths

    # Fake HOME so we don't touch the real ~/.claude
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    # Fake claude binary on PATH
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_claude = fake_bin_dir / "claude"
    fake_claude.write_text("#!/bin/sh\nexit 0\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin_dir))

    # Ambient env var present — must be stripped from child, kept in parent
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-AMBIENT")

    paths = Paths(
        config_dir=tmp_path / "cfg",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )
    paths.state_dir.mkdir(parents=True)

    captured: dict[str, object] = {}

    def _fake_execvpe(file, args, env):
        captured["file"] = file
        captured["args"] = list(args)
        captured["env"] = dict(env)
        raise SystemExit(0)

    with (
        patch("claude_rotate.exec.os.execvpe", side_effect=_fake_execvpe),
        pytest.raises(SystemExit),
    ):
        exec_claude(_acc(), paths, ["hello"])

    # Session breadcrumb written as part of exec (after credentials, before execvpe)
    import json as _json

    session = _json.loads((paths.state_dir / "current-session.json").read_text())
    assert session == {"account_name": "main"}

    # Credentials file written with account's token
    creds = json.loads((home / ".claude" / ".credentials.json").read_text())
    assert creds["claudeAiOauth"]["accessToken"].startswith("sk-ant-oat01-aaaa")
    assert "user:sessions:claude_code" in creds["claudeAiOauth"]["scopes"]

    # Child env does NOT contain CLAUDE_CODE_OAUTH_TOKEN
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["env"]  # type: ignore[operator]
    # Parent env untouched
    assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-AMBIENT"


def test_resolve_claude_binary_finds_real_path(tmp_path, monkeypatch) -> None:
    real = tmp_path / "claude"
    real.write_text("#!/bin/sh\necho ok\n")
    real.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_claude_binary() == str(real)


def test_resolve_claude_binary_skips_self(tmp_path, monkeypatch) -> None:
    """If the only binary found is our own claude-rotate wrapper (symlink back
    to ourselves), raise rather than recurse."""
    rotate = tmp_path / "claude-rotate"
    rotate.write_text("#!/bin/sh\n")
    rotate.chmod(0o755)
    (tmp_path / "claude").symlink_to(rotate)

    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ClaudeBinaryError, match="points back"):
        resolve_claude_binary()


def test_resolve_claude_binary_missing_raises(tmp_path, monkeypatch) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(ClaudeBinaryError, match="not found"):
        resolve_claude_binary()
