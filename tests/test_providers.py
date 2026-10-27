"""Third-party provider quota readers (Codex app server, Antigravity `/usage`).

These providers are display-only: they feed the extra table under the
Anthropic dashboard and must never influence rotation or the exit code.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_rotate import providers
from claude_rotate.config import Paths, ensure_dirs
from claude_rotate.config import paths as config_paths
from claude_rotate.providers import ProviderQuota, ProviderWindow, codex, gemini
from claude_rotate.providers.codex import parse_rate_limits
from claude_rotate.providers.gemini import parse_usage

NOW = datetime(2026, 4, 22, 8, 0, tzinfo=UTC).timestamp()

# Real `agy -p /usage` output: tab-separated, remaining (not used) percent.
AGY_USAGE = (
    "Gemini Models\tWeekly Limit Remaining\t49%\t2026-04-25T18:23:00Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t40%\t2026-04-22T11:23:00Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-04-27T09:59:59Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-04-22T14:59:59Z\n"
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


# Real `account/rateLimits/read` result, trimmed to the fields the reader uses.
CODEX_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": int(NOW) + 7200},
        "secondary": {"usedPercent": 17, "windowDurationMins": 10080, "resetsAt": int(NOW) + 86400},
        "planType": "team",
        "rateLimitReachedType": "workspace_member_usage_limit_reached",
    },
}

FAKE_CODEX = """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "account/rateLimits/read":
        print(json.dumps({"id": msg["id"], "result": %s}), flush=True)
"""


class TestGeminiFetch:
    def test_a_logged_out_agy_yields_no_row_and_no_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Logged out, agy blocks on an OAuth prompt; that is no subscription, not an error."""
        agy = tmp_path / "agy"
        agy.write_text(
            "#!/bin/sh\necho 'Authentication required. Please visit the URL to log in:' >&2\n"
            "exec sleep 30\n"
        )
        agy.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(gemini, "TIMEOUT_SECONDS", 3)

        assert gemini.fetch(NOW) == []


class TestCodexParser:
    def test_reads_both_windows_as_consumed_percent(self) -> None:
        quota = parse_rate_limits(CODEX_RESULT, now=NOW)

        assert quota.provider == "codex"
        assert quota.account == "team"
        assert [(w.label, w.used_pct) for w in quota.windows] == [("5h", 100.0), ("week", 17.0)]
        assert quota.windows[0].reset_secs == 7200

    def test_labels_windows_by_duration_not_position(self) -> None:
        """A plan with only a weekly window reports it as `primary`."""
        result = {"rateLimits": {"primary": {"usedPercent": 3, "windowDurationMins": 10080}}}

        assert [w.label for w in parse_rate_limits(result, now=NOW).windows] == ["week"]

    def test_null_windows_leave_the_row_empty(self) -> None:
        result = {"rateLimits": {"limitId": "premium", "primary": None, "secondary": None}}

        assert parse_rate_limits(result, now=NOW).windows == ()


class TestCodexFetch:
    def _install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
        codex = tmp_path / "codex"
        codex.write_text(script)
        codex.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    def test_asks_the_app_server_and_keeps_stdin_open_for_the_reply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install(tmp_path, monkeypatch, FAKE_CODEX % repr(CODEX_RESULT))

        (quota,) = codex.fetch(NOW)

        assert [w.used_pct for w in quota.windows] == [100.0, 17.0]

    def test_a_silent_server_is_cut_off_and_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install(tmp_path, monkeypatch, "#!/bin/sh\nsleep 30\n")
        monkeypatch.setattr(codex, "TIMEOUT_SECONDS", 0.5)

        (quota,) = codex.fetch(NOW)

        assert quota.windows == ()
        assert "codex" in quota.note

    def test_returns_nothing_when_codex_is_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))

        assert codex.fetch(NOW) == []


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

    def test_cached_never_runs_a_reader(
        self, rotate_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The instant first frame must not wait on agy: valid cache or nothing."""
        paths = self._paths(rotate_dir)

        def boom(now: float) -> list[ProviderQuota]:
            raise AssertionError("cached() must not read live")

        monkeypatch.setattr(providers, "_READERS", (boom,))
        assert providers.cached(paths, now=NOW) == []

        monkeypatch.setattr(
            providers, "_READERS", (lambda now: [ProviderQuota(provider="a", account="a")],)
        )
        providers.collect(paths, now=NOW)
        monkeypatch.setattr(providers, "_READERS", (boom,))
        assert [q.provider for q in providers.cached(paths, now=NOW + 30)] == ["a"]
        assert providers.cached(paths, now=NOW + providers.CACHE_TTL_SECONDS + 1) == []

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
