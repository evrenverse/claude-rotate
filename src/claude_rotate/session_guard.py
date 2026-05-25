"""Session-account guard for Claude Code hooks.

Claude Code stores OAuth credentials globally in ``~/.claude/.credentials.json``.
When multiple long-running sessions are open, a later ``claude-rotate run`` can
switch that global file to another account. If an old long-context session then
continues, Claude Code may have to create a fresh prompt cache under the new
account, burning a large chunk of that account's 5h quota.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths
from claude_rotate.selection import plan_rank
from claude_rotate.sync import read_current_session
from claude_rotate.usage_cache import UsageCache

LONG_CONTEXT_TOKENS = 100_000
UNKNOWN_HEADROOM_CONTEXT_TOKENS = 500_000
CROWDED_H5_PERCENT = 70.0
CROWDED_W7_PERCENT = 80.0
GUARD_ENV_VAR = "CLAUDE_ROTATE_GUARD"
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    account_name: str
    transcript_path: Path
    cwd: str


@dataclass(frozen=True)
class ContextEstimate:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass(frozen=True)
class GuardDecision:
    decision: str
    reason: str = ""

    @classmethod
    def allow(cls) -> GuardDecision:
        return cls(decision="allow")

    @classmethod
    def block(cls, reason: str) -> GuardDecision:
        return cls(decision="block", reason=reason)


def hook_guard_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether global Claude hooks should enforce claude-rotate checks."""
    source = os.environ if env is None else env
    value = source.get(GUARD_ENV_VAR, "")
    return value.strip().lower() in _TRUTHY_ENV_VALUES


def record_session_start(paths: Paths, payload: dict[str, Any]) -> None:
    """Bind a Claude session id to the account selected by ``run``."""
    session_id = _payload_str(payload, "session_id")
    transcript_path = _payload_str(payload, "transcript_path")
    if not session_id or not transcript_path:
        return

    if _load_session_record(paths, session_id) is not None:
        return

    current = read_current_session(paths)
    if current is None:
        return

    record = SessionRecord(
        session_id=session_id,
        account_name=current.account_name,
        transcript_path=Path(transcript_path),
        cwd=_payload_str(payload, "cwd") or "",
    )
    _write_session_record(paths, record)


def evaluate_prompt_submit(
    paths: Paths,
    payload: dict[str, Any],
    *,
    record: SessionRecord | None = None,
) -> GuardDecision:
    """Return a block decision when a long session crossed into a risky account."""
    session_id = _payload_str(payload, "session_id")
    record = record or _load_session_record(paths, session_id)
    if record is None:
        transcript_path = Path(_payload_str(payload, "transcript_path"))
        context = estimate_context(transcript_path)
        if context.prompt_tokens >= LONG_CONTEXT_TOKENS:
            return GuardDecision.block(
                _reason(
                    source=None,
                    target=None,
                    context=context,
                    detail="session is not registered with claude-rotate",
                )
            )
        return GuardDecision.allow()

    active = read_current_session(paths)
    if active is None or active.account_name == record.account_name:
        return GuardDecision.allow()

    transcript_path = Path(_payload_str(payload, "transcript_path") or record.transcript_path)
    context = estimate_context(transcript_path)
    if context.prompt_tokens < LONG_CONTEXT_TOKENS:
        return GuardDecision.allow()

    accounts = Store(paths).load()
    source = accounts.get(record.account_name)
    target = accounts.get(active.account_name)
    if source is None or target is None:
        return GuardDecision.block(
            _reason(
                source=source,
                target=target,
                context=context,
                detail="session account metadata is missing",
            )
        )

    detail = _risk_detail(paths, source=source, target=target, context=context)
    if detail is None:
        return GuardDecision.allow()
    return GuardDecision.block(
        _reason(source=source, target=target, context=context, detail=detail)
    )


def estimate_context(path: Path) -> ContextEstimate:
    """Read the latest assistant usage from a Claude Code transcript."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ContextEstimate()

    for line in reversed(lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = ((raw.get("message") or {}).get("usage") or {})
        if not isinstance(usage, dict):
            continue
        return ContextEstimate(
            input_tokens=_int(usage.get("input_tokens")),
            cache_creation_input_tokens=_int(usage.get("cache_creation_input_tokens")),
            cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
        )
    return ContextEstimate()


def decision_json(decision: GuardDecision) -> str:
    if decision.decision != "block":
        return ""
    return json.dumps({"decision": "block", "reason": decision.reason})


def _risk_detail(
    paths: Paths,
    *,
    source: Account,
    target: Account,
    context: ContextEstimate,
) -> str | None:
    if plan_rank(target.plan) < plan_rank(source.plan):
        return "lower-tier target account"

    cached = UsageCache(paths).load(target.name)
    if cached is None:
        if context.prompt_tokens >= UNKNOWN_HEADROOM_CONTEXT_TOKENS:
            return "target quota headroom is unknown"
        return None

    h5 = cached.h5_pct or 0.0
    w7 = cached.w7_pct or 0.0
    if h5 >= CROWDED_H5_PERCENT or w7 >= CROWDED_W7_PERCENT:
        return "target quota is already crowded"
    return None


def _reason(
    *,
    source: Account | None,
    target: Account | None,
    context: ContextEstimate,
    detail: str,
) -> str:
    source_label = _account_label(source)
    target_label = _account_label(target)
    approx = _format_tokens(context.prompt_tokens)
    return (
        "claude-rotate blocked this prompt to avoid an expensive prompt-cache rewrite. "
        f"Session account: {source_label}. Active account: {target_label}. "
        f"Latest prompt context: ~{approx}. Risk: {detail}. "
        "Continue this session on its original account, or start a fresh session on the "
        "active account."
    )


def _account_label(account: Account | None) -> str:
    if account is None:
        return "<unknown>"
    return f"{account.plan} {account.name}"


def _format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M tokens"
    return f"{round(tokens / 1_000)}k tokens"


def _record_path(paths: Paths, session_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", session_id)
    return paths.state_dir / "sessions" / f"{safe}.json"


def _write_session_record(paths: Paths, record: SessionRecord) -> None:
    path = _record_path(paths, record.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": record.session_id,
        "account_name": record.account_name,
        "transcript_path": str(record.transcript_path),
        "cwd": record.cwd,
        "created_at": int(time.time()),
    }
    path.write_text(json.dumps(payload) + "\n")
    path.chmod(0o600)


def _load_session_record(paths: Paths, session_id: str) -> SessionRecord | None:
    if not session_id:
        return None
    path = _record_path(paths, session_id)
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    account_name = raw.get("account_name")
    transcript_path = raw.get("transcript_path")
    if not isinstance(account_name, str) or not isinstance(transcript_path, str):
        return None
    return SessionRecord(
        session_id=session_id,
        account_name=account_name,
        transcript_path=Path(transcript_path),
        cwd=str(raw.get("cwd", "")),
    )


def _payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0
