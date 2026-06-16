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
    CAPACITY_GATE_THRESHOLD,
    EXPIRY_SOON_DAYS,
    EXPIRY_URGENT_DAYS,
    FORECAST_WINDOW_5H_SECONDS,
    HEADROOM_PERCENT,
    PACE_MIN_ELAPSED_SECONDS,
    SESSION_LOAD_PENALTY,
    SOON_QUOTA_CEILING_PERCENT,
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
    # Opus 7d bucket from the OAuth usage endpoint; None when only the
    # inference rate-limit headers were available.
    w7_opus_pct: float | None = None
    # Weighted live-session load on this account (active + idle*idle_weight),
    # injected by run.py from the session registry. Bridges the probe's blind
    # window so a burst fans out instead of stampeding one account.
    session_load: float = 0.0

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
    w7_opus_pct: float | None = None,
    session_load: float = 0.0,
) -> Candidate:
    return Candidate(
        account=account,
        h5_pct=h5_pct,
        w7_pct=w7_pct,
        h5_reset_secs=h5_reset_secs,
        w7_reset_secs=w7_reset_secs,
        probe_error=probe_error,
        w7_opus_pct=w7_opus_pct,
        session_load=session_load,
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


def _weekly_urgency(c: Candidate) -> float:
    """Weekly headroom-% per hour until the weekly window resets.

    Weekly quota is the scarce good: whatever is unused at reset is forfeited,
    so the account whose remaining weekly quota must be drained fastest scores
    highest. Zero when no weekly data is available.
    """
    if c.w7_pct is None or c.w7_reset_secs <= 0:
        return 0.0
    headroom = max(0.0, HEADROOM_PERCENT - c.w7_pct)
    return headroom / (c.w7_reset_secs / 3600)


def _hourly_urgency(c: Candidate) -> float:
    if c.h5_pct is None or c.h5_reset_secs <= 0:
        return 0.0
    headroom = max(0.0, HEADROOM_PERCENT - c.h5_pct)
    return headroom / (c.h5_reset_secs / 3600)


def _h5_availability(c: Candidate) -> float:
    """Fraction of the 5h window still usable right now (1.0 when unknown).

    The 5h window renews every 5h regardless of which account is picked, so a
    fresh 5h window is no reason to *prefer* an account — but a nearly capped
    one means the pick would stall within minutes, so it dampens the score.
    The stricter of two dampeners wins:

    - Level: 5h headroom left right now.
    - Pace: how much of the time until the 5h reset the account survives at
      its observed burn rate. 47% burned in the first 20 minutes of a window
      walls long before a reset that is hours away — picking that account
      buys minutes, then a multi-hour stall. The same 47% with the reset
      minutes away is harmless (the budget is about to come back).
    """
    if c.h5_pct is None:
        return 1.0
    level = max(0.0, HEADROOM_PERCENT - c.h5_pct) / 100.0
    return min(level, _h5_pace_share(c))


def _h5_pace_share(c: Candidate) -> float:
    """Share of the time until the 5h reset survived at the observed burn rate.

    Time-to-wall is headroom divided by the burn rate (usage per hour since
    the window opened); dividing by the time until reset gives the share of
    the remaining window the account can actually serve. 1.0 when no burn has
    been observed or the pace never hits the wall before the reset.
    """
    if c.h5_pct is None or c.h5_pct <= 0.0 or c.h5_reset_secs <= 0:
        return 1.0
    elapsed_secs = FORECAST_WINDOW_5H_SECONDS - c.h5_reset_secs
    if elapsed_secs < PACE_MIN_ELAPSED_SECONDS:
        return 1.0
    remaining = min(1.0, c.h5_reset_secs / FORECAST_WINDOW_5H_SECONDS)
    elapsed = 1.0 - remaining
    used = c.h5_pct / 100.0
    headroom = max(0.0, HEADROOM_PERCENT - c.h5_pct) / 100.0
    return min(1.0, (headroom * elapsed) / (used * remaining))


def _opus_availability(c: Candidate) -> float:
    """Headroom fraction of the Opus 7d bucket (1.0 when unknown).

    The unified 7d window is the hard wall, but sessions are Opus-first: an
    account whose Opus bucket is nearly capped degrades to non-Opus work even
    with plenty of unified headroom, so it dampens the score the same way a
    nearly capped 5h window does.
    """
    if c.w7_opus_pct is None:
        return 1.0
    return max(0.0, HEADROOM_PERCENT - c.w7_opus_pct) / 100.0


def _session_load_availability(c: Candidate) -> float:
    """Dampener shrinking with the account's recent live-session load.

    Bounded to [0, 1]; never makes an account unusable (is_usable ignores it),
    so a loaded account is only deprioritised. Yields to observed usage: once
    real burn lands in h5_pct, h5_availability compounds with this factor.
    """
    return max(0.0, 1.0 - c.session_load * SESSION_LOAD_PENALTY)


def _capacity_availability(c: Candidate) -> float:
    """Combined capacity dampener in [0, 1]: 5h-window availability × live-session load signal.

    Product of the two dampeners that answer "is there room right now": the 5h wall
    (`_h5_availability` — level and burn-pace) and the live-session stampede signal
    (`_session_load_availability`). Deliberately excludes `_weekly_urgency` (the *reason*
    to drain, not a capacity signal) and `_opus_availability` (a soft degradation, not a
    hard wall). 1.0 when no usage/session data is known.
    """
    return _h5_availability(c) * _session_load_availability(c)


def _drain_urgency_score(c: Candidate) -> float:
    return (
        _weekly_urgency(c)
        * _h5_availability(c)
        * _opus_availability(c)
        * _session_load_availability(c)
    )


def _pick_tier3(usable: list[Candidate]) -> Candidate:
    ranked = sorted(
        usable,
        key=lambda c: (
            -_drain_urgency_score(c),
            -_hourly_urgency(c),
            -plan_rank(c.account.plan),
        ),
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
    # Capacity gate: keep the expiry shortcut only for accounts that can still host
    # another session now. A loaded/walling expiring account is dropped here and
    # falls through to the load/pace-aware tiers, so the current session spills
    # elsewhere instead of stampeding it (its weekly quota still drains across its
    # later 5h windows).
    urgent = [c for c in urgent if _capacity_availability(c) >= CAPACITY_GATE_THRESHOLD]
    if not urgent:
        return None
    urgent.sort(
        key=lambda c: (
            c.subscription_expiry_seconds(now) or 0,
            -plan_rank(c.account.plan),
        )
    )
    return urgent[0]
