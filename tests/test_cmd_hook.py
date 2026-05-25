from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from claude_rotate.accounts import Account, Store
from claude_rotate.commands import hook
from claude_rotate.config import Paths
from claude_rotate.sync import CurrentSession, write_current_session


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
        label=name,
        created_at=datetime(2026, 4, 24, tzinfo=UTC),
        plan=plan,
    )


def _transcript(path: Path, *, cache_read: int) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 6,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": cache_read,
                        "output_tokens": 10,
                    }
                },
            }
        )
        + "\n"
    )


def test_hook_session_start_records_binding(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv("CLAUDE_ROTATE_GUARD", "1")
    write_current_session(paths, CurrentSession(account_name="matri"))

    rc = hook.execute(
        paths,
        "session-start",
        input_text=json.dumps(
            {
                "session_id": "sid",
                "transcript_path": str(tmp_path / "session.jsonl"),
                "cwd": "/repo",
            }
        ),
    )

    assert rc == 0
    raw = json.loads((paths.state_dir / "sessions" / "sid.json").read_text())
    assert raw["account_name"] == "matri"


def test_hook_session_start_skips_without_rotate_guard(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    monkeypatch.delenv("CLAUDE_ROTATE_GUARD", raising=False)
    write_current_session(paths, CurrentSession(account_name="matri"))

    rc = hook.execute(
        paths,
        "session-start",
        input_text=json.dumps(
            {
                "session_id": "sid",
                "transcript_path": str(tmp_path / "session.jsonl"),
                "cwd": "/repo",
            }
        ),
    )

    assert rc == 0
    assert not (paths.state_dir / "sessions" / "sid.json").exists()


def test_hook_user_prompt_submit_outputs_block_json(tmp_path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv("CLAUDE_ROTATE_GUARD", "1")
    transcript = tmp_path / "session.jsonl"
    _transcript(transcript, cache_read=755_000)
    Store(paths).save({"matri": _acc("matri", "max_20x"), "flavius": _acc("flavius", "pro")})
    write_current_session(paths, CurrentSession(account_name="matri"))
    hook.execute(
        paths,
        "session-start",
        input_text=json.dumps(
            {
                "session_id": "sid",
                "transcript_path": str(transcript),
                "cwd": "/repo",
            }
        ),
    )
    write_current_session(paths, CurrentSession(account_name="flavius"))

    rc = hook.execute(
        paths,
        "user-prompt-submit",
        input_text=json.dumps(
            {
                "session_id": "sid",
                "transcript_path": str(transcript),
                "prompt": "mach weiter",
            }
        ),
    )

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "expensive prompt-cache rewrite" in out["reason"]
    assert "max_20x matri" in out["reason"]
    assert "pro flavius" in out["reason"]


def test_hook_user_prompt_submit_skips_without_rotate_guard(tmp_path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    monkeypatch.delenv("CLAUDE_ROTATE_GUARD", raising=False)
    transcript = tmp_path / "session.jsonl"
    _transcript(transcript, cache_read=755_000)
    Store(paths).save({"matri": _acc("matri", "max_20x"), "flavius": _acc("flavius", "pro")})
    write_current_session(paths, CurrentSession(account_name="flavius"))

    rc = hook.execute(
        paths,
        "user-prompt-submit",
        input_text=json.dumps(
            {
                "session_id": "sid",
                "transcript_path": str(transcript),
                "prompt": "continue",
            }
        ),
    )

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_hook_user_prompt_submit_allows_small_session(tmp_path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv("CLAUDE_ROTATE_GUARD", "1")
    transcript = tmp_path / "session.jsonl"
    _transcript(transcript, cache_read=10_000)
    Store(paths).save({"matri": _acc("matri", "max_20x"), "flavius": _acc("flavius", "pro")})
    write_current_session(paths, CurrentSession(account_name="matri"))
    hook.execute(
        paths,
        "session-start",
        input_text=json.dumps(
            {
                "session_id": "sid",
                "transcript_path": str(transcript),
                "cwd": "/repo",
            }
        ),
    )
    write_current_session(paths, CurrentSession(account_name="flavius"))

    rc = hook.execute(
        paths,
        "user-prompt-submit",
        input_text=json.dumps({"session_id": "sid", "transcript_path": str(transcript)}),
    )

    assert rc == 0
    assert capsys.readouterr().out == ""
