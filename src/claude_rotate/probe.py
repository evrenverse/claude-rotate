"""HTTP probe layer — POST /v1/messages and read rate-limit headers."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from claude_rotate.accounts import Account
from claude_rotate.config import (
    ANTHROPIC_BETA,
    ANTHROPIC_VERSION,
    INFERENCE_URL,
    PROBE_MODEL,
    PROBE_TIMEOUT_SECONDS,
    USER_AGENT,
)
from claude_rotate.selection import Candidate, candidate_from_account


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single rate-limit probe."""

    ok: bool
    http_code: int
    h5_pct: float | None = None
    w7_pct: float | None = None
    h5_reset_secs: int = 0
    w7_reset_secs: int = 0
    # Extended fields from the OAuth usage endpoint when available.
    w7_sonnet_pct: float | None = None
    w7_opus_pct: float | None = None
    extra_usage_enabled: bool = False
    error: str = ""
    request_id: str | None = None


def fetch_usage(token: str, *, now: int | None = None) -> ProbeResult:
    """Probe quota using Anthropic's inference rate-limit headers.

    The OAuth usage endpoint can return capped or rounded headline values.
    The inference endpoint exposes the exact unified 5h/7d usage in response
    headers, including on 429 responses where no generation happens.
    """
    now_ts = now if now is not None else int(time.time())
    payload = json.dumps(
        {
            "model": PROBE_MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    req = urllib.request.Request(
        INFERENCE_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
            return parse_rate_limit_headers(resp.status, resp.headers, now=now_ts)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return ProbeResult(ok=False, http_code=e.code, error="unauthorized")
        if e.code == 429:
            parsed = parse_rate_limit_headers(e.code, e.headers, now=now_ts)
            if parsed.ok:
                return parsed
            return ProbeResult(ok=False, http_code=e.code, error="rate_limited")
        if e.code >= 500:
            return ProbeResult(ok=False, http_code=e.code, error="upstream_error")
        return ProbeResult(ok=False, http_code=e.code, error=f"http_{e.code}")
    except (TimeoutError, OSError) as e:
        return ProbeResult(ok=False, http_code=0, error=f"network_error: {e}")


def parse_rate_limit_headers(http_code: int, headers: Any, *, now: int) -> ProbeResult:
    """Parse Anthropic unified rate-limit headers into quota percentages."""

    def _header(name: str) -> str | None:
        value = headers.get(name)
        return str(value) if value is not None else None

    def _pct(name: str) -> float | None:
        value = _header(name)
        if value is None:
            return None
        return float(value) * 100

    def _secs(name: str) -> int:
        value = _header(name)
        if value is None:
            return 0
        return max(0, int(float(value) - now))

    h5_pct = _pct("anthropic-ratelimit-unified-5h-utilization")
    w7_pct = _pct("anthropic-ratelimit-unified-7d-utilization")
    if h5_pct is None and w7_pct is None:
        return ProbeResult(
            ok=False,
            http_code=http_code,
            error="missing_rate_limit_headers",
            request_id=_header("request-id"),
        )

    return ProbeResult(
        ok=True,
        http_code=http_code,
        h5_pct=h5_pct,
        w7_pct=w7_pct,
        h5_reset_secs=_secs("anthropic-ratelimit-unified-5h-reset"),
        w7_reset_secs=_secs("anthropic-ratelimit-unified-7d-reset"),
        request_id=_header("request-id"),
    )


def parse_usage_response(http_code: int, body: dict[str, Any], *, now: int) -> ProbeResult:
    """Pure function: turn the usage JSON into ProbeResult. Unit-testable."""
    import datetime as _dt

    def _secs_until(iso: str | None) -> int:
        if not iso:
            return 0
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return max(0, int(dt.timestamp() - now))

    five = body.get("five_hour") or {}
    seven = body.get("seven_day") or {}
    sonnet = body.get("seven_day_sonnet") or {}
    opus = body.get("seven_day_opus") or {}
    extra = body.get("extra_usage") or {}

    return ProbeResult(
        ok=True,
        http_code=http_code,
        h5_pct=float(five["utilization"]) if "utilization" in five else None,
        w7_pct=float(seven["utilization"]) if "utilization" in seven else None,
        h5_reset_secs=_secs_until(five.get("resets_at")),
        w7_reset_secs=_secs_until(seven.get("resets_at")),
        w7_sonnet_pct=float(sonnet["utilization"]) if sonnet and "utilization" in sonnet else None,
        w7_opus_pct=float(opus["utilization"]) if opus and "utilization" in opus else None,
        extra_usage_enabled=bool(extra.get("is_enabled", False)),
    )


def probe_many(accounts: list[Account]) -> list[Candidate]:
    """Probe every account's quota in parallel.

    Accounts whose probe fails (any non-ok ProbeResult) produce a Candidate
    with None pct values. Callers may then fall back to the usage cache.
    """
    if not accounts:
        return []
    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        results = list(pool.map(lambda a: (a, fetch_usage(a.runtime_token)), accounts))

    out: list[Candidate] = []
    for account, result in results:
        if result.ok:
            out.append(
                candidate_from_account(
                    account,
                    h5_pct=result.h5_pct,
                    w7_pct=result.w7_pct,
                    h5_reset_secs=result.h5_reset_secs,
                    w7_reset_secs=result.w7_reset_secs,
                    probe_error="",
                )
            )
        else:
            out.append(
                candidate_from_account(
                    account,
                    h5_pct=None,
                    w7_pct=None,
                    h5_reset_secs=0,
                    w7_reset_secs=0,
                    probe_error=result.error,
                )
            )
    return out
