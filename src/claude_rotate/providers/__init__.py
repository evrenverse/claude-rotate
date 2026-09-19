"""Quota readers for the other coding subscriptions on this machine.

Display-only by design. These providers feed the extra table under the
Anthropic dashboard; they never enter ``selection``, never influence
rotation, and a failure among them never changes ``status``'s exit code —
those codes describe Anthropic account health and nothing else.

Each provider module exposes ``fetch(now) -> list[ProviderQuota]`` and
returns an empty list when the tool simply is not installed. A tool that
*is* present but fails yields one quota with empty ``windows`` and the
reason in ``note``, so a broken reader is visible rather than silent.
"""

from __future__ import annotations

import contextlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from claude_rotate.config import Paths, atomic_write_json

_CACHE_NAME = "providers.json"


@dataclass(frozen=True)
class ProviderWindow:
    """One rate-limit window, in the dashboard's vocabulary: consumed percent."""

    label: str  # "5h" | "week"
    used_pct: float
    reset_secs: int


@dataclass(frozen=True)
class ProviderQuota:
    provider: str  # "codex" | "gemini"
    account: str  # plan or model family the windows belong to
    windows: tuple[ProviderWindow, ...] = ()
    note: str = ""


# Long enough that ``--watch`` (5s cadence) never waits on agy's ~7s startup,
# short enough that a hand-run ``status`` after a long session tells the truth.
CACHE_TTL_SECONDS = 60


def _readers() -> tuple[Any, ...]:
    from claude_rotate.providers import codex, gemini

    return (codex.fetch, gemini.fetch)


# None means "use the real readers"; tests substitute their own tuple, and an
# empty tuple has to stay meaningful — hence a sentinel rather than a falsy test.
_READERS: tuple[Any, ...] | None = None


def collect(paths: Paths, *, now: float | None = None) -> list[ProviderQuota]:
    """Every provider's quota, cached for ``CACHE_TTL_SECONDS``.

    Readers run in parallel — one slow tool must not serialize the others —
    and each is wrapped: a provider that raises drops out of the table
    instead of taking the dashboard with it.
    """
    now = time.time() if now is None else now
    cached = _read_cache(paths, now)
    if cached is not None:
        return cached

    readers = _readers() if _READERS is None else _READERS
    quotas: list[ProviderQuota] = []
    if readers:
        with ThreadPoolExecutor(max_workers=len(readers)) as pool:
            for future in [pool.submit(_safely, r, now) for r in readers]:
                quotas.extend(future.result())

    _write_cache(paths, quotas, now)
    return quotas


def _safely(reader: Any, now: float) -> list[ProviderQuota]:
    try:
        return list(reader(now))
    except Exception:
        return []


def _read_cache(paths: Paths, now: float) -> list[ProviderQuota] | None:
    """Cached quotas with reset clocks wound forward, or None when stale."""
    try:
        raw = json.loads((paths.usage_dir / _CACHE_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    age = now - float(raw.get("fetched_at", 0))
    if not 0 <= age < CACHE_TTL_SECONDS:
        return None
    return [
        ProviderQuota(
            provider=q["provider"],
            account=q["account"],
            windows=tuple(
                ProviderWindow(
                    label=w["label"],
                    used_pct=w["used_pct"],
                    # Stored absolute so the countdown keeps running while cached.
                    reset_secs=max(0, int(w["reset_at"] - now)),
                )
                for w in q["windows"]
            ),
            note=q.get("note", ""),
        )
        for q in raw.get("quotas", [])
    ]


def _write_cache(paths: Paths, quotas: list[ProviderQuota], now: float) -> None:
    payload = {
        "fetched_at": now,
        "quotas": [
            {
                "provider": q.provider,
                "account": q.account,
                "note": q.note,
                "windows": [
                    {"label": w.label, "used_pct": w.used_pct, "reset_at": now + w.reset_secs}
                    for w in q.windows
                ],
            }
            for q in quotas
        ],
    }
    with contextlib.suppress(OSError):
        atomic_write_json(paths.usage_dir / _CACHE_NAME, payload)
