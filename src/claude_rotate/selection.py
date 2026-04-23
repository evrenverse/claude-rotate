"""Account-selection heuristic — pure, no I/O.

Three tiers plus a fallback, as documented in the spec. Every input is an
already-probed `Candidate`. Output is a chosen `Account` plus an optional
human-readable "wait" message when all accounts are exhausted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from claude_rotate.accounts import Account
from claude_rotate.config import (
    BALANCE_THRESHOLD_PERCENT,
    EXPIRY_SOON_DAYS,
    EXPIRY_URGENT_DAYS,
    HEADROOM_PERCENT,
    HOURLY_WEIGHT,
    SOON_QUOTA_CEILING_PERCENT,
    WEEKLY_WEIGHT,
)

_PLAN_RANKS: dict[str, int] = {
    "max_20x": 2,
    "max_5x": 1,
    "pro": 0,
}


@dataclass(frozen=True)
class Candidate:
    """An account paired with its last-known usage numbers."""

    account: Account
    h5_pct: float | None
    w7_pct: float | None
    h5_reset_secs: int
    w7_reset_secs: int
    probe_error: str = ""

    def subscription_expiry_seconds(self, now: datetime | None = None) -> int | None:
        # effective_expires_at: manual override wins over API-derived
        expires = self.account.effective_expires_at
        if expires is None:
            return None
        now = now or datetime.now(UTC)
        delta = expires - now
        return max(0, int(delta.total_seconds()))


def candidate_from_account(
    account: Account,
    h5_pct: float | None,
    w7_pct: float | None,
    h5_reset_secs: int,
    w7_reset_secs: int,
    probe_error: str = "",
) -> Candidate:
    return Candidate(
        account=account,
        h5_pct=h5_pct,
        w7_pct=w7_pct,
        h5_reset_secs=h5_reset_secs,
        w7_reset_secs=w7_reset_secs,
        probe_error=probe_error,
    )


def plan_rank(plan: str) -> int:
    return _PLAN_RANKS.get(plan, -1)


def is_usable(c: Candidate) -> bool:
    return (c.h5_pct is None or c.h5_pct < HEADROOM_PERCENT) and (
        c.w7_pct is None or c.w7_pct < HEADROOM_PERCENT
    )


def next_available_seconds(c: Candidate) -> int:
    """Seconds until this candidate's limiting window clears.

    Zero if already usable. If multiple windows are capped, returns the
    later one.
    """
    secs = 0
    if c.h5_pct is not None and c.h5_pct >= HEADROOM_PERCENT:
        secs = max(secs, c.h5_reset_secs)
    if c.w7_pct is not None and c.w7_pct >= HEADROOM_PERCENT:
        secs = max(secs, c.w7_reset_secs)
    return secs


def _soonest_reset_seconds(c: Candidate) -> int:
    """Seconds until the first capped window clears (minimum of capped resets).

    Returns 0 when no window is capped. Used in the exhausted fallback to pick
    the account that will regain any capacity soonest.
    """
    caps: list[int] = []
    if c.h5_pct is not None and c.h5_pct >= HEADROOM_PERCENT:
        caps.append(c.h5_reset_secs)
    if c.w7_pct is not None and c.w7_pct >= HEADROOM_PERCENT:
        caps.append(c.w7_reset_secs)
    return min(caps) if caps else 0


def pick_best(
    candidates: list[Candidate],
    now: datetime | None = None,
) -> tuple[Candidate, str | None]:
    """Return (best, wait_message). Wait message non-None when everyone is exhausted.

    Tiers are resolved in order; the first tier that matches returns.
    """
    if not candidates:
        raise ValueError("pick_best requires at least one candidate")

    now = now or datetime.now(UTC)
    usable = [c for c in candidates if is_usable(c)]

    # Tier 1 — urgent subscription expiry
    tier1 = _pick_tier1(usable, now)
    if tier1 is not None:
        return tier1, None

    # Tier 2 — weekly balance guard
    tier2 = _pick_tier2(usable, now)
    if tier2 is not None:
        return tier2, None

    # Tier 3 — drain urgency
    if usable:
        return _pick_tier3(usable), None

    # Fallback — everyone is exhausted
    ranked = sorted(
        candidates,
        key=lambda c: (_soonest_reset_seconds(c), -plan_rank(c.account.plan)),
    )
    best = ranked[0]
    wait_seconds = _soonest_reset_seconds(best)
    return best, _format_wait(best, wait_seconds)


def _drain_urgency_score(c: Candidate) -> float:
    h5 = c.h5_pct or 0.0
    w7 = c.w7_pct or 0.0
    score = 0.0
    if c.h5_pct is not None and c.h5_reset_secs > 0:
        headroom = max(0.0, HEADROOM_PERCENT - h5)
        score += (headroom / (c.h5_reset_secs / 3600)) * HOURLY_WEIGHT
    if c.w7_pct is not None and c.w7_reset_secs > 0:
        headroom = max(0.0, HEADROOM_PERCENT - w7)
        score += (headroom / (c.w7_reset_secs / 3600)) * WEEKLY_WEIGHT
    return score


def _pick_tier3(usable: list[Candidate]) -> Candidate:
    ranked = sorted(
        usable,
        key=lambda c: (-_drain_urgency_score(c), -plan_rank(c.account.plan)),
    )
    return ranked[0]


def _pick_tier2(usable: list[Candidate], now: datetime) -> Candidate | None:
    w7_vals = [c.w7_pct for c in usable if c.w7_pct is not None]
    if not w7_vals:
        return None
    spread = max(w7_vals) - min(w7_vals)
    if spread <= BALANCE_THRESHOLD_PERCENT:
        return None

    # Exception: soon-expiring with still-reasonable weekly headroom wins
    soon_with_quota = []
    for c in usable:
        secs = c.subscription_expiry_seconds(now)
        w7 = c.w7_pct or 0.0
        if (
            secs is not None
            and 0 < secs <= EXPIRY_SOON_DAYS * 86400
            and w7 < SOON_QUOTA_CEILING_PERCENT
        ):
            soon_with_quota.append(c)
    if soon_with_quota:
        soon_with_quota.sort(key=lambda c: c.subscription_expiry_seconds(now) or 0)
        return soon_with_quota[0]

    return _pick_tier3(usable)


def _format_wait(c: Candidate, secs: int) -> str:
    hours, rem = divmod(secs, 3600)
    minutes = rem // 60
    if hours > 0:
        return f"all accounts exhausted; {c.account.label} available in {hours}h {minutes:02d}m"
    return f"all accounts exhausted; {c.account.label} available in {minutes}m"


def _pick_tier1(usable: list[Candidate], now: datetime) -> Candidate | None:
    urgent = []
    for c in usable:
        secs = c.subscription_expiry_seconds(now)
        if secs is not None and 0 < secs <= EXPIRY_URGENT_DAYS * 86400:
            urgent.append(c)
    if not urgent:
        return None
    urgent.sort(
        key=lambda c: (
            c.subscription_expiry_seconds(now) or 0,
            -plan_rank(c.account.plan),
        )
    )
    return urgent[0]
