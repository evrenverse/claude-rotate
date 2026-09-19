"""Third-party provider quota readers (Codex rollouts, Antigravity `/usage`).

These providers are display-only: they feed the extra table under the
Anthropic dashboard and must never influence rotation or the exit code.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_rotate import providers
from claude_rotate.config import Paths, ensure_dirs
from claude_rotate.config import paths as config_paths
from claude_rotate.providers import ProviderQuota, ProviderWindow
from claude_rotate.providers.codex import parse_rollout
from claude_rotate.providers.gemini import parse_usage

NOW = datetime(2026, 4, 22, 8, 0, tzinfo=UTC).timestamp()

# Real `agy -p /usage` output: tab-separated, remaining (not used) percent.
AGY_USAGE = (
    "Gemini Models\tWeekly Limit Remaining\t49%\t2026-04-25T18:23:00Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t40%\t2026-04-22T11:23:00Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-04-27T09:59:59Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-04-22T14:59:59Z\n"
)


def _rollout_line(ts: str, *, primary: float, secondary: float, resets_at: int) -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": primary,
                        "window_minutes": 300,
                        "resets_at": resets_at,
                    },
                    "secondary": {
                        "used_percent": secondary,
                        "window_minutes": 10080,
                        "resets_at": resets_at + 600_000,
                    },
                    "plan_type": "team",
                },
            },
        }
    )


class TestGeminiParser:
    def test_reports_usage_as_consumed_not_remaining(self) -> None:
        """49% remaining is 51% used — the dashboard speaks in consumed quota."""
        quotas = parse_usage(AGY_USAGE, now=NOW)

        gemini = next(q for q in quotas if q.account == "Gemini Models")
        week = next(w for w in gemini.windows if w.label == "week")
        assert week.used_pct == 51.0

    def test_groups_every_model_family_into_its_own_row(self) -> None:
        quotas = parse_usage(AGY_USAGE, now=NOW)

        assert [q.account for q in quotas] == ["Gemini Models", "Claude and GPT models"]
        assert all(q.provider == "gemini" for q in quotas)

    def test_orders_windows_five_hour_before_week(self) -> None:
        """Matches the Anthropic dashboard's column order."""
        gemini = parse_usage(AGY_USAGE, now=NOW)[0]

        assert [w.label for w in gemini.windows] == ["5h", "week"]

    def test_converts_absolute_reset_stamp_to_seconds_remaining(self) -> None:
        gemini = parse_usage(AGY_USAGE, now=NOW)[0]
        five_hour = next(w for w in gemini.windows if w.label == "5h")

        assert five_hour.reset_secs == 3 * 3600 + 23 * 60  # 08:00Z -> 11:23Z

    def test_ignores_lines_that_are_not_quota_rows(self) -> None:
        """agy may print progress chatter before the table."""
        noisy = "Loading workspace...\n\n" + AGY_USAGE + "Done.\n"

        assert len(parse_usage(noisy, now=NOW)) == 2


class TestCodexParser:
    def test_reads_the_last_rate_limit_event_in_the_session(self) -> None:
        """A session logs rate limits per turn; only the newest one is current."""
        lines = [
            _rollout_line("2026-04-22T07:00:00Z", primary=1.0, secondary=10.0, resets_at=int(NOW)),
            _rollout_line(
                "2026-04-22T07:30:00Z", primary=42.0, secondary=13.0, resets_at=int(NOW) + 7200
            ),
        ]

        quota = parse_rollout(lines, now=NOW)

        assert quota is not None
        assert [w.used_pct for w in quota.windows] == [42.0, 13.0]

    def test_labels_the_row_with_the_plan_type(self) -> None:
        quota = parse_rollout(
            [_rollout_line("2026-04-22T07:30:00Z", primary=1.0, secondary=1.0, resets_at=int(NOW))],
            now=NOW,
        )

        assert quota is not None
        assert quota.provider == "codex"
        assert quota.account == "team"

    def test_treats_an_elapsed_window_as_reset_to_zero(self) -> None:
        """Rollouts are historical: a window whose reset has passed is empty now."""
        stale = _rollout_line(
            "2026-04-21T07:30:00Z", primary=88.0, secondary=20.0, resets_at=int(NOW) - 3600
        )

        quota = parse_rollout([stale], now=NOW)

        assert quota is not None
        five_hour = next(w for w in quota.windows if w.label == "5h")
        assert five_hour.used_pct == 0.0
        assert five_hour.reset_secs == 0

    def test_marks_measurements_older_than_a_few_minutes_as_stale(self) -> None:
        """Codex numbers are only as fresh as the last codex session."""
        lines = [
            _rollout_line(
                "2026-04-22T06:00:00Z", primary=42.0, secondary=13.0, resets_at=int(NOW) + 7200
            )
        ]

        quota = parse_rollout(lines, now=NOW)

        assert quota is not None
        assert quota.note == "measured 2h 0m ago"

    def test_stays_silent_when_the_session_logged_no_rate_limits(self) -> None:
        assert (
            parse_rollout(['{"type":"event_msg","payload":{"type":"agent_message"}}'], now=NOW)
            is None
        )

    def test_survives_a_truncated_final_line(self) -> None:
        """Rollouts of a running session end mid-write."""
        good = _rollout_line(
            "2026-04-22T07:55:00Z", primary=5.0, secondary=5.0, resets_at=int(NOW) + 60
        )

        quota = parse_rollout([good, '{"timestamp": "2026-04-22T07:5'], now=NOW)

        assert quota is not None
        assert quota.windows[0].used_pct == 5.0


class TestProviderQuota:
    def test_a_failed_provider_carries_its_error_without_windows(self) -> None:
        failed = ProviderQuota(provider="gemini", account="gemini", windows=(), note="timeout")

        assert failed.windows == ()
        assert failed.note == "timeout"


class TestCollect:
    """``collect`` fans the readers out in parallel and caches the result."""

    def _paths(self, rotate_dir: Path) -> Paths:
        paths = config_paths()
        ensure_dirs(paths)
        return paths

    def test_runs_every_provider_and_returns_all_quotas(
        self, rotate_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            providers,
            "_READERS",
            (
                lambda now: [ProviderQuota(provider="a", account="a")],
                lambda now: [ProviderQuota(provider="b", account="b")],
            ),
        )

        quotas = providers.collect(self._paths(rotate_dir), now=NOW)

        assert sorted(q.provider for q in quotas) == ["a", "b"]

    def test_a_crashing_provider_does_not_take_down_the_others(
        self, rotate_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One broken reader must never cost you the whole extra table."""

        def boom(now: float) -> list[ProviderQuota]:
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(
            providers, "_READERS", (boom, lambda now: [ProviderQuota(provider="b", account="b")])
        )

        quotas = providers.collect(self._paths(rotate_dir), now=NOW)

        assert [q.provider for q in quotas] == ["b"]

    def test_serves_a_fresh_cache_without_running_the_providers(
        self, rotate_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keeps ``--watch`` fluid: agy needs ~7s, the cache answers instantly."""
        paths = self._paths(rotate_dir)
        monkeypatch.setattr(
            providers, "_READERS", (lambda now: [ProviderQuota(provider="a", account="first")],)
        )
        providers.collect(paths, now=NOW)

        monkeypatch.setattr(
            providers, "_READERS", (lambda now: [ProviderQuota(provider="a", account="second")],)
        )
        quotas = providers.collect(paths, now=NOW + 30)

        assert [q.account for q in quotas] == ["first"]

    def test_refetches_once_the_cache_has_expired(
        self, rotate_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = self._paths(rotate_dir)
        monkeypatch.setattr(
            providers, "_READERS", (lambda now: [ProviderQuota(provider="a", account="first")],)
        )
        providers.collect(paths, now=NOW)

        monkeypatch.setattr(
            providers, "_READERS", (lambda now: [ProviderQuota(provider="a", account="second")],)
        )
        quotas = providers.collect(paths, now=NOW + providers.CACHE_TTL_SECONDS + 1)

        assert [q.account for q in quotas] == ["second"]

    def test_cached_windows_survive_the_round_trip(
        self, rotate_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reset clocks count down while cached — they are stored absolute."""
        paths = self._paths(rotate_dir)
        monkeypatch.setattr(
            providers,
            "_READERS",
            (
                lambda now: [
                    ProviderQuota(
                        provider="a",
                        account="a",
                        windows=(ProviderWindow(label="5h", used_pct=42.0, reset_secs=600),),
                        note="hi",
                    )
                ],
            ),
        )
        providers.collect(paths, now=NOW)

        monkeypatch.setattr(providers, "_READERS", ())
        (quota,) = providers.collect(paths, now=NOW + 30)

        assert quota.note == "hi"
        assert quota.windows[0].used_pct == 42.0
        assert quota.windows[0].reset_secs == 570
