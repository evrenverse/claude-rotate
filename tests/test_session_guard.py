from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths
from claude_rotate.probe import ProbeResult
from claude_rotate.session_guard import (
    GuardDecision,
    SessionRecord,
    evaluate_prompt_submit,
    record_session_start,
)
from claude_rotate.sync import CurrentSession, write_current_session
from claude_rotate.usage_cache import UsageCache


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def _acc(name: str, plan: str) -> Account:
    return Account(
        name=name,
        runtime_token=f"sk-ant-oat01-{name}",
        label=f"{plan} {name}",
        created_at=datetime(2026, 4, 24, tzinfo=UTC),
        plan=plan,
        email=f"{name}@example.com",
    )


def _transcript(path: Path, *, cache_read: int = 0, cache_creation: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "assistant",
        "timestamp": "2026-04-24T15:53:25.446Z",
        "message": {
            "model": "claude-opus-4-7",
            "usage": {
                "input_tokens": 6,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": 1220,
            },
        },
    }
    path.write_text(json.dumps(payload) + "\n")


def test_record_session_start_binds_session_to_current_account(tmp_path) -> None:
    paths = _paths(tmp_path)
    write_current_session(paths, CurrentSession(account_name="matri"))

    record_session_start(
        paths,
        {
            "session_id": "abc123",
            "transcript_path": str(tmp_path / "session.jsonl"),
            "cwd": "/repo",
        },
    )

    raw = json.loads((paths.state_dir / "sessions" / "abc123.json").read_text())
    assert raw["account_name"] == "matri"
    assert raw["transcript_path"].endswith("session.jsonl")
    assert raw["cwd"] == "/repo"


def test_record_session_start_does_not_overwrite_existing_binding(tmp_path) -> None:
    paths = _paths(tmp_path)
    transcript = tmp_path / "session.jsonl"
    write_current_session(paths, CurrentSession(account_name="matri"))
    record_session_start(
        paths,
        {
            "session_id": "abc123",
            "transcript_path": str(transcript),
            "cwd": "/repo",
        },
    )
    write_current_session(paths, CurrentSession(account_name="flavius"))

    record_session_start(
        paths,
        {
            "session_id": "abc123",
            "transcript_path": str(transcript),
            "cwd": "/repo",
        },
    )

    raw = json.loads((paths.state_dir / "sessions" / "abc123.json").read_text())
    assert raw["account_name"] == "matri"


def test_large_session_blocks_when_active_account_is_lower_tier(tmp_path) -> None:
    paths = _paths(tmp_path)
    transcript = tmp_path / "session.jsonl"
    _transcript(transcript, cache_read=755_000)
    Store(paths).save(
        {
            "matri": _acc("matri", "max_20x"),
            "flavius": _acc("flavius", "pro"),
        }
    )
    write_current_session(paths, CurrentSession(account_name="flavius"))

    decision = evaluate_prompt_submit(
        paths,
        {
            "session_id": "abc123",
            "transcript_path": str(transcript),
        },
        record=SessionRecord(
            session_id="abc123",
            account_name="matri",
            transcript_path=transcript,
            cwd="/repo",
        ),
    )

    assert decision.decision == "block"
    assert "lower-tier target account" in decision.reason
    assert "max_20x matri" in decision.reason
    assert "pro flavius" in decision.reason
    assert "~755k" in decision.reason


def test_unregistered_large_session_blocks(tmp_path) -> None:
    paths = _paths(tmp_path)
    transcript = tmp_path / "session.jsonl"
    _transcript(transcript, cache_read=755_000)
    write_current_session(paths, CurrentSession(account_name="matri"))

    decision = evaluate_prompt_submit(
        paths,
        {
            "session_id": "abc123",
            "transcript_path": str(transcript),
        },
    )

    assert decision.decision == "block"
    assert "not registered" in decision.reason
    assert "~755k" in decision.reason


def test_large_session_allows_upgrade_to_fresh_max_account(tmp_path) -> None:
    paths = _paths(tmp_path)
    transcript = tmp_path / "session.jsonl"
    _transcript(transcript, cache_read=755_000)
    Store(paths).save(
        {
            "pro1": _acc("pro1", "pro"),
            "max1": _acc("max1", "max_20x"),
        }
    )
    UsageCache(paths).save(
        "max1",
        ProbeResult(
            ok=True,
            http_code=200,
            h5_pct=5.0,
            w7_pct=10.0,
            h5_reset_secs=1,
            w7_reset_secs=1,
        ),
    )
    write_current_session(paths, CurrentSession(account_name="max1"))

    decision = evaluate_prompt_submit(
        paths,
        {
            "session_id": "abc123",
            "transcript_path": str(transcript),
        },
        record=SessionRecord(
            session_id="abc123",
            account_name="pro1",
            transcript_path=transcript,
            cwd="/repo",
        ),
    )

    assert decision == GuardDecision.allow()


def test_large_session_blocks_when_target_quota_is_crowded(tmp_path) -> None:
    paths = _paths(tmp_path)
    transcript = tmp_path / "session.jsonl"
    _transcript(transcript, cache_read=300_000)
    Store(paths).save(
        {
            "max1": _acc("max1", "max_20x"),
            "max2": _acc("max2", "max_20x"),
        }
    )
    UsageCache(paths).save(
        "max2",
        ProbeResult(
            ok=True,
            http_code=200,
            h5_pct=80.0,
            w7_pct=20.0,
            h5_reset_secs=3600,
            w7_reset_secs=86400,
        ),
    )
    write_current_session(paths, CurrentSession(account_name="max2"))

    decision = evaluate_prompt_submit(
        paths,
        {
            "session_id": "abc123",
            "transcript_path": str(transcript),
        },
        record=SessionRecord(
            session_id="abc123",
            account_name="max1",
            transcript_path=transcript,
            cwd="/repo",
        ),
    )

    assert decision.decision == "block"
    assert "target quota is already crowded" in decision.reason
