"""Claude Code hook entry points for claude-rotate."""

from __future__ import annotations

import json
import sys
from typing import Any

from claude_rotate.config import Paths
from claude_rotate.session_guard import (
    decision_json,
    evaluate_prompt_submit,
    hook_guard_enabled,
    record_session_start,
)


def execute(paths: Paths, event: str, *, input_text: str | None = None) -> int:
    payload = _read_payload(input_text)
    if event == "session-start":
        if not hook_guard_enabled():
            return 0
        record_session_start(paths, payload)
        return 0
    if event == "user-prompt-submit":
        if not hook_guard_enabled():
            return 0
        decision = evaluate_prompt_submit(paths, payload)
        out = decision_json(decision)
        if out:
            print(out)
        return 0
    print(f"error: unknown hook event: {event}", file=sys.stderr)
    return 2


def _read_payload(input_text: str | None) -> dict[str, Any]:
    text = input_text if input_text is not None else sys.stdin.read()
    try:
        raw = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}
