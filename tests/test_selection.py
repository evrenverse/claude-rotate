from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from claude_rotate.accounts import Account
from claude_rotate.selection import (
    Candidate,
    _capacity_availability,
    is_usable,
    next_available_seconds,
    pick_best,
    plan_rank,
)


def _acc(
    name: str = "main",
    plan: str = "max_20x",
    subscription_expires_at: datetime | None = None,
) -> Account:
    return Account(
        name=name,
        runtime_token="sk-ant-oat01-" + "a" * 96,
        label=name,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        plan=plan,
        subscription_expires_at=subscription_expires_at,
    )


# Reference "now" used by both fixture construction and pick_best() calls
# below. Without pinning this, tests that build accounts with small
# ``expires_days`` values flake the day the wall-clock crosses the
# synthetic expiry. See pick_best(..., now=FIXED_NOW) callers.
FIXED_NOW = datetime(2026, 4, 22, tzinfo=UTC)


def _cand(
    h5: float | None = 10.0,
    w7: float | None = 10.0,
    h5_secs: int = 3600,
    w7_secs: int = 86400,
    plan: str = "max_20x",
    expires_days: int | None = None,
) -> Candidate:
    subscription_expires_at = (
        FIXED_NOW + timedelta(days=expires_days) if expires_days is not None else None
    )
    return Candidate(
        account=_acc(plan=plan, subscription_expires_at=subscription_expires_at),
        h5_pct=h5,
        w7_pct=w7,
        h5_reset_secs=h5_secs,
        w7_reset_secs=w7_secs,
    )


def test_plan_rank_ordering() -> None:
    assert plan_rank("max_20x") > plan_rank("max_5x") > plan_rank("pro")
    assert plan_rank("unknown") == -1


def test_is_usable_below_headroom() -> None:
    assert is_usable(_cand(h5=50.0, w7=50.0))


def test_is_usable_false_at_5h_cap() -> None:
    # Only 100% utilisation is considered truly exhausted — Anthropic's
    # hard limit is 100%, so we burn accounts all the way down rather
    # than leave quota on the table.
    assert not is_usable(_cand(h5=100.0, w7=10.0))


def test_is_usable_false_at_weekly_cap() -> None:
    assert not is_usable(_cand(h5=10.0, w7=100.0))


def test_is_usable_true_at_99_percent() -> None:
    """<100% still counts — match Anthropic's actual rate-limit boundary."""
    assert is_usable(_cand(h5=99.0, w7=99.0))
    assert is_usable(_cand(h5=95.0, w7=95.0))


def test_is_usable_none_values_treated_as_ok() -> None:
    assert is_usable(_cand(h5=None, w7=None))


def test_next_available_zero_when_usable() -> None:
    assert next_available_seconds(_cand(h5=10.0, w7=10.0)) == 0


def test_next_available_returns_5h_when_only_5h_capped() -> None:
    c = _cand(h5=100.0, w7=10.0, h5_secs=3600, w7_secs=86400)
    assert next_available_seconds(c) == 3600


def test_next_available_returns_max_when_both_capped() -> None:
    c = _cand(h5=100.0, w7=100.0, h5_secs=3600, w7_secs=86400)
    assert next_available_seconds(c) == 86400


def test_tier1_picks_urgent_expiring_account() -> None:
    urgent = _cand(plan="max_20x", expires_days=2, h5=10.0, w7=10.0)
    normal = _cand(plan="max_20x", expires_days=None, h5=5.0, w7=5.0)
    chosen, wait = pick_best([urgent, normal], now=FIXED_NOW)
    assert chosen.account is urgent.account
    assert wait is None


def test_tier1_prefers_earliest_expiry_when_multiple_urgent() -> None:
    near = _cand(plan="max_20x", expires_days=1, h5=10.0, w7=10.0)
    far = _cand(plan="max_20x", expires_days=3, h5=5.0, w7=5.0)
    chosen, _ = pick_best([far, near], now=FIXED_NOW)
    assert chosen.account is near.account


def test_tier1_uses_plan_rank_as_tiebreaker_on_equal_expiry() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    exp = now + timedelta(days=2)
    hi = Candidate(
        account=Account(
            name="hi",
            runtime_token="sk-ant-oat01-" + "a" * 96,
            label="hi",
            created_at=now,
            plan="max_20x",
            subscription_expires_at=exp,
        ),
        h5_pct=10.0,
        w7_pct=10.0,
        h5_reset_secs=3600,
        w7_reset_secs=86400,
    )
    lo = Candidate(
        account=Account(
            name="lo",
            runtime_token="sk-ant-oat01-" + "b" * 96,
            label="lo",
            created_at=now,
            plan="pro",
            subscription_expires_at=exp,
        ),
        h5_pct=10.0,
        w7_pct=10.0,
        h5_reset_secs=3600,
        w7_reset_secs=86400,
    )
    chosen, _ = pick_best([lo, hi], now=FIXED_NOW)
    assert chosen.account.plan == "max_20x"


def test_tier1_skips_exhausted_accounts() -> None:
    exhausted_urgent = _cand(plan="max_20x", expires_days=2, h5=100.0, w7=50.0)
    healthy = _cand(plan="max_20x", expires_days=None, h5=5.0, w7=5.0)
    chosen, _ = pick_best([exhausted_urgent, healthy], now=FIXED_NOW)
    assert chosen.account is healthy.account


def test_tier2_balance_picks_lower_weekly_when_spread_exceeds_threshold() -> None:
    high = _cand(plan="max_20x", w7=80.0, h5=10.0)
    low = _cand(plan="max_20x", w7=20.0, h5=10.0)
    chosen, _ = pick_best([high, low], now=FIXED_NOW)
    assert chosen.account is low.account


def test_tier2_balance_skipped_when_spread_below_threshold() -> None:
    # Spread 15pp < 30pp threshold → tier 3 decides → drain-urgency wins
    a = _cand(plan="max_20x", w7=40.0, h5=10.0, w7_secs=86400)
    b = _cand(plan="max_20x", w7=55.0, h5=10.0, w7_secs=86400)
    chosen, _ = pick_best([a, b], now=FIXED_NOW)
    # With equal h5 and similar w7, the lower-weekly one still has more headroom,
    # so tier 3 picks it. The point is tier 2 is NOT triggered.
    assert chosen.account is a.account


def test_tier2_soon_expiring_with_quota_wins_over_balance() -> None:
    soon_low_quota = _cand(plan="max_20x", w7=60.0, h5=10.0, expires_days=8)  # soon <10d, quota <70
    balance_winner = _cand(plan="max_20x", w7=5.0, h5=10.0)
    chosen, _ = pick_best([balance_winner, soon_low_quota], now=FIXED_NOW)
    assert chosen.account is soon_low_quota.account


def test_tier2_soon_expiring_at_ceiling_does_NOT_win_over_balance() -> None:
    # soon <10d BUT weekly ≥70% → SOON_QUOTA_CEILING rejects it, balance wins
    soon_at_ceiling = _cand(plan="max_20x", w7=75.0, h5=10.0, expires_days=8)
    balance_winner = _cand(plan="max_20x", w7=5.0, h5=10.0)
    chosen, _ = pick_best([balance_winner, soon_at_ceiling], now=FIXED_NOW)
    assert chosen.account is balance_winner.account


def test_tier3_prefers_higher_headroom_per_reset_hour() -> None:
    # Balanced weekly (<30pp spread) → tier 3 kicks in.
    # Candidate A: 10% weekly utilisation, resets in 24h → ample headroom
    # Candidate B: 40% weekly utilisation, resets in 24h → less headroom
    a = _cand(plan="max_20x", w7=10.0, h5=10.0, h5_secs=3600, w7_secs=86400)
    b = _cand(plan="max_20x", w7=40.0, h5=10.0, h5_secs=3600, w7_secs=86400)
    chosen, _ = pick_best([b, a], now=FIXED_NOW)
    assert chosen.account is a.account


def test_tier3_weekly_urgency_beats_fresher_5h_window() -> None:
    # B's weekly window resets in 24h with 85% headroom left — quota that is
    # about to be forfeited. A merely has a fresher 5h window, which renews
    # every 5h anyway. B must win.
    a = _cand(plan="max_20x", h5=10.0, w7=10.0, h5_secs=7800, w7_secs=120 * 3600)
    b = _cand(plan="max_20x", h5=50.0, w7=15.0, h5_secs=7800, w7_secs=24 * 3600)
    chosen, _ = pick_best([a, b], now=FIXED_NOW)
    assert chosen.account is b.account


def test_tier3_real_world_regression_soon_resetting_weekly_wins() -> None:
    # 2026-06-10 live snapshot: stamp's weekly resets in 26h at 19% used,
    # grace's in 94h at 15%. The old weighted-sum score let grace's fresher
    # 5h window (16% vs 39%) overrule stamp's far higher weekly urgency.
    grace = _cand(h5=16.0, w7=15.0, h5_secs=7800, w7_secs=94 * 3600)
    matri = _cand(h5=47.0, w7=22.0, h5_secs=7800, w7_secs=112 * 3600, expires_days=8)
    stamp = _cand(h5=39.0, w7=19.0, h5_secs=7800, w7_secs=26 * 3600)
    chosen, _ = pick_best([grace, matri, stamp], now=FIXED_NOW)
    assert chosen.account is stamp.account


def test_tier3_hot_young_5h_window_yields_to_fresh_accounts() -> None:
    # 2026-06-10 live snapshot (afternoon): stamp burned 47% of its 5h budget
    # in the first ~20 minutes of the window (4h41m until reset — projection
    # →764%). Its weekly resets in 23h41m, so pure weekly urgency would pick
    # it, but at that pace it walls within minutes and then stalls for hours
    # while grace/matri sit on fresh 5h windows. The pace dampener must hand
    # the pick over; stamp gets drained again once its 5h window resets.
    stamp = _cand(h5=47.0, w7=35.0, h5_secs=16860, w7_secs=85260)
    grace = _cand(h5=0.0, w7=28.0, h5_secs=16860, w7_secs=330060)
    matri = _cand(h5=0.0, w7=30.0, h5_secs=16860, w7_secs=394860, expires_days=8)
    chosen, _ = pick_best([stamp, grace, matri], now=FIXED_NOW)
    assert chosen.account is grace.account


def test_tier3_young_window_low_use_not_pace_dampened() -> None:
    # A window opened 2 minutes ago with 3% used projects >100% numerically,
    # but that pace estimate is pure noise (one short session / probe burst).
    # Below the minimum elapsed time only the level dampener applies, so the
    # account with the far more urgent weekly reset must still win.
    hot_weekly = _cand(h5=3.0, w7=35.0, h5_secs=17880, w7_secs=85260)
    fresh = _cand(h5=0.0, w7=28.0, h5_secs=17880, w7_secs=330060)
    chosen, _ = pick_best([hot_weekly, fresh], now=FIXED_NOW)
    assert chosen.account is hot_weekly.account


def test_tier3_same_usage_near_5h_reset_keeps_drain_priority() -> None:
    # Same 47% 5h usage, but the window resets in 20 minutes — the burned
    # budget is about to come back, so weekly drain urgency must still win.
    stamp = _cand(h5=47.0, w7=35.0, h5_secs=1200, w7_secs=85260)
    grace = _cand(h5=0.0, w7=28.0, h5_secs=1200, w7_secs=330060)
    chosen, _ = pick_best([stamp, grace], now=FIXED_NOW)
    assert chosen.account is stamp.account


def test_tier3_near_capped_5h_dampens_equal_weekly_urgency() -> None:
    # Equal weekly urgency → the account that can actually work right now
    # (more 5h headroom) wins.
    blocked = _cand(plan="max_20x", h5=90.0, w7=20.0, h5_secs=7200, w7_secs=86400)
    free = _cand(plan="max_20x", h5=10.0, w7=20.0, h5_secs=7200, w7_secs=86400)
    chosen, _ = pick_best([blocked, free], now=FIXED_NOW)
    assert chosen.account is free.account


def test_tier3_capped_opus_weekly_dampens_equal_weekly_urgency() -> None:
    # Equal unified urgency, but one account's Opus 7d bucket is nearly
    # capped — sessions there degrade to non-Opus work, so the account
    # with Opus headroom must win.
    opus_capped = _cand(plan="max_20x", h5=10.0, w7=20.0, h5_secs=7200, w7_secs=86400)
    opus_capped = replace(opus_capped, w7_opus_pct=95.0)
    opus_free = _cand(plan="max_20x", h5=10.0, w7=20.0, h5_secs=7200, w7_secs=86400)
    opus_free = replace(opus_free, w7_opus_pct=10.0)
    chosen, _ = pick_best([opus_capped, opus_free], now=FIXED_NOW)
    assert chosen.account is opus_free.account


def test_tier3_missing_opus_data_changes_nothing() -> None:
    # No Opus data (inference-header-only probe) → dampener is neutral and
    # the ranking is decided by the unified windows alone.
    a = _cand(plan="max_20x", w7=10.0, h5=10.0, h5_secs=3600, w7_secs=86400)
    b = _cand(plan="max_20x", w7=40.0, h5=10.0, h5_secs=3600, w7_secs=86400)
    assert a.w7_opus_pct is None and b.w7_opus_pct is None
    chosen, _ = pick_best([b, a], now=FIXED_NOW)
    assert chosen.account is a.account


def test_tier3_no_weekly_data_falls_back_to_hourly_urgency() -> None:
    a = _cand(plan="max_20x", h5=60.0, w7=None, h5_secs=7200, w7_secs=0)
    b = _cand(plan="max_20x", h5=20.0, w7=None, h5_secs=7200, w7_secs=0)
    chosen, _ = pick_best([a, b], now=FIXED_NOW)
    assert chosen.account is b.account


def test_fallback_returns_earliest_available_when_all_exhausted() -> None:
    a = _cand(plan="max_20x", h5=100.0, w7=100.0, h5_secs=3600, w7_secs=86400)
    b = _cand(plan="max_20x", h5=100.0, w7=100.0, h5_secs=300, w7_secs=86400)
    chosen, wait = pick_best([a, b], now=FIXED_NOW)
    assert chosen.account is b.account
    assert wait is not None
    assert "exhausted" in wait.lower()


def test_fallback_wait_message_contains_duration() -> None:
    a = _cand(plan="max_20x", h5=100.0, w7=100.0, h5_secs=3600, w7_secs=3600)
    _chosen, wait = pick_best([a], now=FIXED_NOW)
    assert wait is not None
    assert "1h" in wait or "3600" in wait


# ---------------------------------------------------------------------------
# Issue 1: Candidate.probe_error field
# ---------------------------------------------------------------------------


def test_candidate_accepts_probe_error_default_empty() -> None:
    """Candidate defaults probe_error to '' when not specified."""
    c = _cand()
    assert c.probe_error == ""


def test_candidate_accepts_probe_error_rate_limited() -> None:
    """Candidate stores a non-empty probe_error when explicitly set."""
    from claude_rotate.selection import candidate_from_account

    acc = _acc()
    c = candidate_from_account(
        acc,
        h5_pct=None,
        w7_pct=None,
        h5_reset_secs=0,
        w7_reset_secs=0,
        probe_error="rate_limited",
    )
    assert c.probe_error == "rate_limited"


def test_candidate_accepts_probe_error_unauthorized() -> None:
    """Candidate stores 'unauthorized' probe_error."""
    from claude_rotate.selection import candidate_from_account

    acc = _acc()
    c = candidate_from_account(
        acc,
        h5_pct=None,
        w7_pct=None,
        h5_reset_secs=0,
        w7_reset_secs=0,
        probe_error="unauthorized",
    )
    assert c.probe_error == "unauthorized"


def test_session_load_deprioritises_busy_account() -> None:
    # Two equal candidates; only the second carries live-session load.
    free = replace(_cand(h5=10.0, w7=10.0), account=_acc(name="free"))
    busy = replace(_cand(h5=10.0, w7=10.0), account=_acc(name="busy"), session_load=4.0)
    best, wait = pick_best([busy, free], now=FIXED_NOW)
    assert wait is None
    assert best.account.name == "free"


def test_session_load_does_not_affect_usability() -> None:
    from claude_rotate.selection import is_usable

    loaded = replace(_cand(h5=10.0, w7=10.0), session_load=99.0)
    assert is_usable(loaded) is True


def test_session_load_availability_curve() -> None:
    from claude_rotate.selection import _session_load_availability

    assert _session_load_availability(replace(_cand(), session_load=0.0)) == 1.0
    # penalty 0.25 → load 2 → 0.5
    assert _session_load_availability(replace(_cand(), session_load=2.0)) == 0.5
    # never negative
    assert _session_load_availability(replace(_cand(), session_load=99.0)) == 0.0


def test_session_load_partial_deprioritises_but_keeps_positive_score() -> None:
    # A partial load (2.0 → multiplier 0.5) deprioritises busy below free,
    # yet busy keeps a positive drain score (not zeroed like the saturated case).
    from claude_rotate.selection import _drain_urgency_score

    free = replace(_cand(h5=10.0, w7=10.0), account=_acc(name="free"))
    busy = replace(_cand(h5=10.0, w7=10.0), account=_acc(name="busy"), session_load=2.0)
    assert _drain_urgency_score(busy) > 0.0
    assert _drain_urgency_score(busy) < _drain_urgency_score(free)
    best, wait = pick_best([busy, free], now=FIXED_NOW)
    assert wait is None
    assert best.account.name == "free"


def test_loaded_account_still_picked_when_sole_usable() -> None:
    # Heavy load (drain score 0.0) must NOT make an account unpickable when it
    # is the only usable candidate — the dampener only deprioritises.
    only = replace(_cand(h5=10.0, w7=10.0), account=_acc(name="only"), session_load=99.0)
    best, wait = pick_best([only], now=FIXED_NOW)
    assert wait is None
    assert best.account.name == "only"


def test_capacity_availability_is_product_of_h5_and_load() -> None:
    # h5 at 80% with 3600s left -> level=(100-80)/100=0.20; pace=1.0 (rate exactly fills remaining budget)
    # session_load 2.4 -> _session_load_availability = 1 - 2.4*0.25 = 0.40
    c = replace(_cand(h5=80.0, w7=10.0), session_load=2.4)
    assert _capacity_availability(c) == 0.20 * 0.40


def test_capacity_availability_is_one_without_data() -> None:
    # No h5 usage and no session load -> both dampeners 1.0
    # session_load defaults to 0.0 on Candidate -> load dampener also 1.0
    c = _cand(h5=None, w7=10.0)
    assert _capacity_availability(c) == 1.0
