"""HTTP probe layer — GET /api/oauth/usage."""

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
    USER_AGENT,
)
from claude_rotate.selection import Candidate, candidate_from_account

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of a single rate-limit probe."""

    ok: bool
    http_code: int
    h5_pct: float | None = None
    w7_pct: float | None = None
    h5_reset_secs: int = 0
    w7_reset_secs: int = 0
    # Extended fields from the usage endpoint
    w7_sonnet_pct: float | None = None
    w7_opus_pct: float | None = None
    extra_usage_enabled: bool = False
    error: str = ""
    request_id: str | None = None


def fetch_usage(token: str, *, now: int | None = None) -> ProbeResult:
    """GET /api/oauth/usage and parse into ProbeResult.

    Replaces the old probe_usage() which POSTed to /v1/messages and read
    rate-limit headers. This endpoint is read-only, quota-free, and returns
    strictly more information (per-model usage, extra-usage state).
    """
    now_ts = now if now is not None else int(time.time())
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            code = resp.status
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return ProbeResult(ok=False, http_code=e.code, error="unauthorized")
        if e.code == 429:
            return ProbeResult(ok=False, http_code=e.code, error="rate_limited")
        if e.code >= 500:
            return ProbeResult(ok=False, http_code=e.code, error="upstream_error")
        return ProbeResult(ok=False, http_code=e.code, error=f"http_{e.code}")
    except (TimeoutError, OSError) as e:
        return ProbeResult(ok=False, http_code=0, error=f"network_error: {e}")

    return parse_usage_response(code, body, now=now_ts)


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
