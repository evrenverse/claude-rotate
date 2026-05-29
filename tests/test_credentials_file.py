from __future__ import annotations

import json
import json as _json
import time
from pathlib import Path

import pytest

from claude_rotate.credentials_file import (
    CredentialsFile,
    CredentialsPayload,
    read_credentials,
    write_credentials,
)

FIXTURE_FULL = Path(__file__).parent / "fixtures" / "credentials_full_scope.json"
FIXTURE_INFER = Path(__file__).parent / "fixtures" / "credentials_inference_only.json"


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_write_creates_file_with_mode_600(tmp_path, monkeypatch) -> None:
    _fake_home(tmp_path, monkeypatch)
    payload = CredentialsPayload(
        access_token="sk-ant-oat01-" + "a" * 100,
        refresh_token="sk-ant-ort01-" + "b" * 100,
        expires_at_ms=1776959432367,
        scopes=["user:profile", "user:inference"],
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
    )
    cf = CredentialsFile()
    cf.write(payload)
    assert cf.path.exists()
    mode = cf.path.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_produces_expected_json_shape(tmp_path, monkeypatch) -> None:
    _fake_home(tmp_path, monkeypatch)
    payload = CredentialsPayload(
        access_token="sk-ant-oat01-TEST",
        refresh_token="sk-ant-ort01-TEST",
        expires_at_ms=1776959432367,
        scopes=[
            "user:profile",
            "user:inference",
            "user:sessions:claude_code",
            "user:mcp_servers",
            "user:file_upload",
        ],
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
    )
    CredentialsFile().write(payload)
    loaded = json.loads(CredentialsFile().path.read_text())
    assert loaded == {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-TEST",
            "refreshToken": "sk-ant-ort01-TEST",
            "expiresAt": 1776959432367,
            "scopes": [
                "user:profile",
                "user:inference",
                "user:sessions:claude_code",
                "user:mcp_servers",
                "user:file_upload",
            ],
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
        }
    }


def test_write_does_not_back_up_existing_file(tmp_path, monkeypatch) -> None:
    """write() overwrites in place — it must never snapshot the old file.

    The previous behaviour wrote one ``.bak-*`` per rotation/refresh, which
    piled up hundreds of stale token copies between prunes.
    """
    home = _fake_home(tmp_path, monkeypatch)
    existing = home / ".claude" / ".credentials.json"
    existing.write_text('{"original": true}')
    existing.chmod(0o600)

    payload = CredentialsPayload(
        access_token="sk-ant-oat01-" + "a" * 100,
        refresh_token=None,
        expires_at_ms=1776959432367,
        scopes=["user:inference"],
        subscription_type="unknown",
        rate_limit_tier=None,
    )
    CredentialsFile().write(payload)

    backups = list((home / ".claude").glob(".credentials.json.bak-*"))
    assert backups == []


def test_write_atomic_no_tmp_leftover(tmp_path, monkeypatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    existing = home / ".claude" / ".credentials.json"
    existing.write_text('{"before": true}')
    existing.chmod(0o600)

    payload = CredentialsPayload(
        access_token="sk-ant-oat01-" + "z" * 100,
        refresh_token=None,
        expires_at_ms=1776959432367,
        scopes=["user:inference"],
        subscription_type="unknown",
        rate_limit_tier=None,
    )
    CredentialsFile().write(payload)
    tmp_files = list((home / ".claude").glob(".credentials.json.tmp-*"))
    assert tmp_files == []


def test_read_returns_none_when_missing(tmp_path, monkeypatch) -> None:
    _fake_home(tmp_path, monkeypatch)
    assert read_credentials() is None


def test_read_roundtrip_full_scope(tmp_path, monkeypatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    target = home / ".claude" / ".credentials.json"
    target.write_text(FIXTURE_FULL.read_text())
    target.chmod(0o600)

    payload = read_credentials()
    assert payload is not None
    assert payload.access_token.startswith("sk-ant-oat01-FULL_SCOPE_")
    assert payload.refresh_token is not None
    assert payload.refresh_token.startswith("sk-ant-ort01-FULL_SCOPE_")
    assert payload.expires_at_ms == 1776959432367
    assert "user:sessions:claude_code" in payload.scopes


def test_read_handles_null_refresh_token(tmp_path, monkeypatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    target = home / ".claude" / ".credentials.json"
    target.write_text(FIXTURE_INFER.read_text())
    target.chmod(0o600)

    payload = read_credentials()
    assert payload is not None
    assert payload.refresh_token is None


def test_write_removes_leftover_backups(tmp_path, monkeypatch) -> None:
    """write() sweeps up any ``.bak-*`` left by older versions, regardless of age."""
    home = _fake_home(tmp_path, monkeypatch)
    now = int(time.time())
    old_backup = home / ".claude" / f".credentials.json.bak-{now - 30 * 86400}"
    old_backup.write_text("{}")
    recent_backup = home / ".claude" / f".credentials.json.bak-{now - 60}"
    recent_backup.write_text("{}")

    payload = CredentialsPayload(
        access_token="sk-ant-oat01-" + "a" * 100,
        refresh_token=None,
        expires_at_ms=1776959432367,
        scopes=["user:inference"],
        subscription_type="unknown",
        rate_limit_tier=None,
    )
    CredentialsFile().write(payload)

    assert list((home / ".claude").glob(".credentials.json.bak-*")) == []


def test_write_credentials_to_explicit_dir(tmp_path: Path) -> None:
    target_dir = tmp_path / "configs" / "matri"
    target_dir.mkdir(parents=True)
    payload = CredentialsPayload(
        access_token="sk-ant-oat01-AAA",
        refresh_token="sk-ant-ort01-BBB",
        expires_at_ms=1_700_000_000_000,
        scopes=["user:inference"],
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
    )
    write_credentials(payload, config_dir=target_dir)
    written = _json.loads((target_dir / ".credentials.json").read_text())
    assert written["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-AAA"
    assert ((target_dir / ".credentials.json").stat().st_mode & 0o777) == 0o600
