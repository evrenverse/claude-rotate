"""Regression tests for the four issues found in the 2026-08-06 review.

Each test fails against the pre-fix code:

1. ``metadata.refresh_stale_accounts`` / ``sync.reconcile_*`` saved a whole
   account map built from a stale read, rolling back a token another process
   had rotated in between (→ spent refresh token → revoked family → relogin).
2. ``CredentialsFile.read`` raised on a corrupt file, killing the cron tick
   before its proactive refresh ran.
3. Account names arriving as CLI arguments reached the store unvalidated and
   are used as path components.
4. ``compute_forecast`` truncated ``pct`` before projecting, flattening
   sub-1% usage to a 0% forecast.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_rotate import metadata, sync
from claude_rotate.accounts import Account, Store
from claude_rotate.config import Paths, paths
from claude_rotate.credentials_file import CredentialsFile, CredentialsPayload
from claude_rotate.errors import AccountError
from claude_rotate.insights import compute_forecast
from claude_rotate.probe import ProbeResult

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _account(name: str, **over: object) -> Account:
    base = Account(
        name=name,
        runtime_token="sk-ant-oat01-" + "a" * 100,
        label=f"Max-20 {name}",
        created_at=NOW,
        plan="max_20x",
        email=f"{name}@example.com",
        refresh_token="sk-ant-ort01-original",
        runtime_token_obtained_at=NOW,
        refresh_token_obtained_at=NOW,
        metadata_refreshed_at=NOW,
    )
    return replace(base, **over)  # type: ignore[arg-type]


@pytest.fixture
def store_paths(rotate_dir: Path) -> Paths:
    p = paths()
    p.config_dir.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 1. Lock race — a concurrent rotation must survive
# ---------------------------------------------------------------------------


def test_metadata_refresh_keeps_token_rotated_during_probe(
    store_paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token rotated while the probes run must not be rolled back.

    ``refresh_stale_accounts`` reads the store, spends seconds on HTTP, then
    writes. We simulate the cron rotating the refresh token inside that window
    and assert the rotated value survives while the metadata still lands.
    """
    store = Store(store_paths)
    stale = NOW - timedelta(days=30)
    store.save({"work": _account("work", metadata_refreshed_at=stale)})

    def rotating_probe(token: str, **_: object) -> ProbeResult:
        # Stand-in for the cron tick that rotates tokens mid-probe.
        current = store.load()
        store.save(
            {
                "work": replace(
                    current["work"],
                    runtime_token="sk-ant-oat01-ROTATED",
                    refresh_token="sk-ant-ort01-ROTATED",
                )
            }
        )
        return ProbeResult(ok=True, http_code=200, h5_pct=10.0, w7_pct=20.0)

    monkeypatch.setattr(metadata, "fetch_usage", rotating_probe)
    monkeypatch.setattr(
        "claude_rotate.oauth.fetch_profile",
        lambda _t: type("P", (), {"ok": False, "error": "skipped"})(),
    )

    metadata.refresh_stale_accounts(store_paths, now=NOW)

    saved = store.load()["work"]
    assert saved.refresh_token == "sk-ant-ort01-ROTATED", "concurrent rotation was rolled back"
    assert saved.runtime_token == "sk-ant-oat01-ROTATED"
    assert saved.metadata_refreshed_at == NOW, "metadata update was lost"


def test_metadata_refresh_skips_account_replaced_under_same_name(
    store_paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handle re-used by a different account must not inherit the old metadata.

    Merging by name alone would copy the removed account's email/plan onto the
    freshly logged-in one; ``created_at`` distinguishes them.
    """
    store = Store(store_paths)
    stale = NOW - timedelta(days=30)
    store.save({"work": _account("work", metadata_refreshed_at=stale)})

    def replacing_probe(token: str, **_: object) -> ProbeResult:
        # Stand-in for `remove work && login other@example.com work` mid-probe.
        store.save(
            {
                "work": _account(
                    "work",
                    email="other@example.com",
                    created_at=NOW + timedelta(seconds=1),
                    metadata_refreshed_at=None,
                )
            }
        )
        return ProbeResult(ok=True, http_code=200, h5_pct=1.0, w7_pct=2.0)

    monkeypatch.setattr(metadata, "fetch_usage", replacing_probe)
    monkeypatch.setattr(
        "claude_rotate.oauth.fetch_profile",
        lambda _t: type("P", (), {"ok": False, "error": "skipped"})(),
    )

    metadata.refresh_stale_accounts(store_paths, now=NOW)

    saved = store.load()["work"]
    assert saved.email == "other@example.com", "metadata was misattributed to the new account"
    assert saved.metadata_refreshed_at is None, "the removed account's refresh stamp leaked over"


def test_reconcile_once_holds_the_lock(store_paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """reconcile_once must do its read-modify-write inside the store lock."""
    store = Store(store_paths)
    store.save({"work": _account("work")})
    sync.write_current_session(store_paths, sync.CurrentSession(account_name="work"))

    seen: list[str] = []
    real_locked = Store.locked

    def tracking_locked(self: Store):  # type: ignore[no-untyped-def]
        seen.append("locked")
        return real_locked(self)

    monkeypatch.setattr(Store, "locked", tracking_locked)

    changed = sync.reconcile_once(
        CredentialsPayload(
            access_token="sk-ant-oat01-NEW",
            refresh_token="sk-ant-ort01-NEW",
            expires_at_ms=0,
            scopes=[],
            subscription_type="max",
            rate_limit_tier=None,
        ),
        store_paths,
        now=NOW,
    )

    assert changed is True
    assert seen == ["locked"], "reconcile_once wrote without holding the lock"
    assert store.load()["work"].refresh_token == "sk-ant-ort01-NEW"


# ---------------------------------------------------------------------------
# 2. Corrupt credentials file must read as "nothing to sync"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        '{"claudeAiOauth": {"accessTok',  # truncated mid-write
        "{}",  # valid JSON, missing the oauth object
        '{"claudeAiOauth": {"refreshToken": "x"}}',  # missing accessToken
        "[]",  # valid JSON, wrong shape
    ],
    ids=["truncated", "no-oauth-key", "no-access-token", "wrong-shape"],
)
def test_corrupt_credentials_file_reads_as_none(tmp_path: Path, content: str) -> None:
    (tmp_path / ".credentials.json").write_text(content)
    assert CredentialsFile(tmp_path).read() is None


def test_reconcile_all_survives_corrupt_credentials(
    store_paths: Paths, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cron entry point must not abort before its proactive refresh."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"accessTok')
    monkeypatch.setenv("HOME", str(home))

    Store(store_paths).save({"work": _account("work")})
    sync.write_current_session(store_paths, sync.CurrentSession(account_name="work"))

    assert sync.reconcile_all(store_paths, now=NOW) is False


# ---------------------------------------------------------------------------
# 3. Account-name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "a/b",
        ".",
        "..",
        "with space",
        "",
        "tab\t",
        # Python's ``$`` also matches before a trailing newline, so an anchored
        # ``re.match`` would let these through — "..\n" even past the explicit
        # relative-path check. Regression guard for that bypass.
        "work\n",
        "..\n",
        ".\n",
    ],
)
def test_unsafe_account_names_rejected(name: str) -> None:
    from claude_rotate.accounts import validate_account_name

    with pytest.raises(AccountError):
        validate_account_name(name)


@pytest.mark.parametrize("name", ["work", "work-2", "work_2", "work.private", "A1"])
def test_safe_account_names_accepted(name: str) -> None:
    from claude_rotate.accounts import validate_account_name

    assert validate_account_name(name) == name


def test_build_account_rejects_path_traversal() -> None:
    from claude_rotate.login import build_account

    with pytest.raises(AccountError):
        build_account(
            name="../../evil",
            token="sk-ant-oat01-" + "a" * 100,
            email="user@example.com",
            plan="max_20x",
            now=NOW,
        )


# ---------------------------------------------------------------------------
# 4. Forecast must not flatten sub-1% usage
# ---------------------------------------------------------------------------


def test_forecast_keeps_sub_one_percent_usage() -> None:
    """0.7% burned in half the window projects to ~1%, not 0%."""
    window, reset = 18000, 9000  # half the 5h window elapsed
    assert compute_forecast(0.7, reset, window) == 1


def test_forecast_unchanged_for_whole_percentages() -> None:
    window, reset = 18000, 9000
    assert compute_forecast(50.0, reset, window) == 100
    assert compute_forecast(20.0, reset, window) == 40


def test_forecast_still_zero_for_zero_usage() -> None:
    assert compute_forecast(0.0, 9000, 18000) == 0


def test_credentials_roundtrip_still_works(tmp_path: Path) -> None:
    """The read() guard must not swallow a valid file."""
    payload = CredentialsPayload(
        access_token="sk-ant-oat01-valid",
        refresh_token="sk-ant-ort01-valid",
        expires_at_ms=1234,
        scopes=["user:inference"],
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
    )
    CredentialsFile(tmp_path).write(payload)
    assert json.loads((tmp_path / ".credentials.json").read_text())["claudeAiOauth"]
    assert CredentialsFile(tmp_path).read() == payload


# ---------------------------------------------------------------------------
# 5. Follow-up review: a failed reconcile must not be followed by a refresh
# ---------------------------------------------------------------------------


def test_sync_credentials_skips_refresh_when_reconcile_is_locked_out(
    store_paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contended lock must abort the whole tick, not just the reconcile.

    accounts.json may still hold a refresh token the running session already
    spent; sending it would trip reuse detection and revoke the family.
    """
    from claude_rotate.commands import sync_credentials
    from claude_rotate.errors import LockTimeoutError

    Store(store_paths).save({"work": _account("work")})

    def locked_out(*_a: object, **_k: object) -> list[str]:
        raise LockTimeoutError("held by another writer")

    refreshed_called = []
    monkeypatch.setattr(sync_credentials, "reconcile_all", locked_out)
    monkeypatch.setattr(
        sync_credentials,
        "refresh_stale_tokens",
        lambda *a, **k: refreshed_called.append(True) or [],
    )

    assert sync_credentials.execute(store_paths) == 0
    assert refreshed_called == [], "refreshed tokens despite an un-reconciled store"


# ---------------------------------------------------------------------------
# 6. Follow-up review: a scoped-only cache entry is not usable quota data
# ---------------------------------------------------------------------------


def test_row_from_cache_rejects_entry_without_usage(store_paths: Paths) -> None:
    """``update_scoped`` writes an entry with no percentages — not a fallback.

    Treating it as data would put the account back into the selection pool as
    "usable" with entirely unknown quota.
    """
    from claude_rotate.dashboard import row_from_cache
    from claude_rotate.selection import ScopedLimit, candidate_from_account
    from claude_rotate.usage_cache import UsageCache

    cache = UsageCache(store_paths)
    cache.update_scoped("work", (ScopedLimit(label="fable", pct=5.0, reset_secs=3600),))

    candidate = candidate_from_account(
        _account("work"),
        h5_pct=None,
        w7_pct=None,
        h5_reset_secs=0,
        w7_reset_secs=0,
        probe_error="rate_limited",
    )
    filled, row = row_from_cache(candidate, cache, probe_error="rate_limited")

    assert filled is None, "scoped-only cache entry was treated as usable quota"
    assert row.status == "no_data"


# ---------------------------------------------------------------------------
# 7. Follow-up review: forecast and limit-ETA must agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pct", [50.5, 60.0, 75.25, 99.5])
def test_forecast_over_100_always_has_an_eta(pct: float) -> None:
    """forecast >= 100 must come with an ETA inside the horizon.

    compute_forecast stopped truncating pct; compute_limit_eta still did, so
    50.5% rendered "→101%" with an empty ETA column.
    """
    from claude_rotate.insights import compute_forecast, compute_limit_eta

    forecast = compute_forecast(pct, 9000, 18000)
    eta = compute_limit_eta(pct, 9000, 18000)
    assert forecast is not None and forecast >= 100
    assert eta is not None, f"forecast {forecast}% but no ETA for pct={pct}"
    assert eta < 9000


def test_forecast_exactly_100_has_no_eta() -> None:
    """The boundary: hitting 100% exactly at reset never blocks before it."""
    from claude_rotate.insights import compute_forecast, compute_limit_eta

    assert compute_forecast(50.0, 9000, 18000) == 100
    assert compute_limit_eta(50.0, 9000, 18000) is None
