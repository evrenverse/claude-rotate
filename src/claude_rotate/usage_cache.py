"""On-disk cache of the last successful rate-limit probe per account.

Used as a fallback when a live probe fails (429, timeout, 5xx). The cache
remembers absolute reset timestamps so we can extrapolate seconds-remaining
at read time. Entries older than MAX_CACHE_AGE are ignored.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from claude_rotate.config import (
    USAGE_HISTORY_MAX_POINTS,
    USAGE_HISTORY_RETENTION_SECONDS,
    Paths,
    atomic_write_json,
)
from claude_rotate.probe import ProbeResult
from claude_rotate.selection import ScopedLimit

MAX_CACHE_AGE_SECONDS = 10 * 60

# History sample layout: [probed_at, h5_pct, w7_pct]. The pct columns map to the
# ``window`` argument of ``recent_rate`` ("5h" -> index 1, "7d" -> index 2).
_WINDOW_COL = {"5h": 1, "7d": 2}


def _serialize_scoped(scoped: tuple[ScopedLimit, ...], now: float) -> list[list[Any]]:
    """ScopedLimits -> cache entries ``[label, pct, reset_at, fetched_at]``.

    Live values were fetched now; a stale value being re-saved keeps its
    original fetch time (``None`` = unknown/legacy).
    """

    def fetched_at(s: ScopedLimit) -> float | None:
        if not s.stale:
            return now
        return now - s.age_secs if s.age_secs is not None else None

    return [[s.label, s.pct, now + s.reset_secs, fetched_at(s)] for s in scoped]


def _parse_scoped_entry(entry: object, now: float) -> ScopedLimit | None:
    """Turn a cached ``[label, pct, reset_at, fetched_at?]`` entry into a ScopedLimit.

    Cache-served values are always ``stale`` — they were fetched by an earlier
    probe. ``fetched_at`` yields ``age_secs``; 3-element entries predate the
    fetch timestamp, so their age stays ``None`` (stale, unknown age). An
    elapsed reset window zeroes the pct (usage has reset).
    """
    if not isinstance(entry, list) or len(entry) not in (3, 4):
        return None
    label, pct, reset_at = entry[0], entry[1], entry[2]
    fetched_at = entry[3] if len(entry) == 4 else None
    secs = max(0, int(float(reset_at) - now))
    return ScopedLimit(
        label=str(label),
        pct=0.0 if secs == 0 else float(pct),
        reset_secs=secs,
        stale=True,
        age_secs=max(0, int(now - float(fetched_at))) if fetched_at is not None else None,
    )


class UsageCache:
    def __init__(self, paths: Paths) -> None:
        self._paths = paths

    def _path_for(self, name: str) -> Path:
        return self._paths.usage_dir / f"{name}.json"

    def _history_path(self, name: str) -> Path:
        return self._paths.usage_dir / f"{name}.history.json"

    def _read_raw(self, name: str) -> dict[str, Any] | None:
        path = self._path_for(name)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        return raw if isinstance(raw, dict) else None

    def load_scoped(self, name: str) -> tuple[ScopedLimit, ...]:
        """Last known model-scoped weekly limits, kept until their own reset.

        Unlike ``load``, this ignores MAX_CACHE_AGE: a scoped weekly limit only
        moves through the account's own usage, so the last fetched value stays a
        valid lower bound until the window resets. Used to backfill a probe
        whose OAuth usage fetch failed (the endpoint rate-limits aggressively).
        Served entries are marked ``stale`` and carry their fetch age so
        renderers can flag them instead of passing them off as fresh.
        """
        raw = self._read_raw(name)
        if raw is None:
            return ()
        now = time.time()
        scoped: list[ScopedLimit] = []
        for entry in raw.get("w7_scoped") or []:
            s = _parse_scoped_entry(entry, now)
            if s is None or s.reset_secs == 0:
                continue  # malformed, or window elapsed — the scoped usage has reset
            scoped.append(s)
        return tuple(scoped)

    def load(self, name: str) -> ProbeResult | None:
        raw = self._read_raw(name)
        if raw is None:
            return None

        now = time.time()
        probed_at = float(raw.get("probed_at", 0))

        def _secs(reset_at: float) -> int:
            return max(0, int(reset_at - now))

        h5_reset_at = float(raw.get("h5_reset_at", 0))
        w7_reset_at = float(raw.get("w7_reset_at", 0))
        h5_secs = _secs(h5_reset_at)
        w7_secs = _secs(w7_reset_at)

        # Only enforce the staleness cap while we are still within a reset
        # window. Once all windows have elapsed the usage has reset to zero,
        # so the entry is still valid (and will be returned with zeroed pcts).
        all_windows_elapsed = h5_secs == 0 and w7_secs == 0
        if not all_windows_elapsed and now - probed_at > MAX_CACHE_AGE_SECONDS:
            return None

        h5_pct = raw.get("h5_pct")
        w7_pct = raw.get("w7_pct")
        w7_opus_pct = raw.get("w7_opus_pct")
        if h5_pct is not None and h5_secs == 0:
            h5_pct = 0.0
        if w7_pct is not None and w7_secs == 0:
            w7_pct = 0.0
        if w7_opus_pct is not None and w7_secs == 0:
            # The Opus bucket lives inside the 7d cadence; once the weekly
            # window elapsed its usage has reset as well.
            w7_opus_pct = 0.0

        scoped: list[ScopedLimit] = []
        for entry in raw.get("w7_scoped") or []:
            s = _parse_scoped_entry(entry, now)
            if s is not None:
                scoped.append(s)

        return ProbeResult(
            ok=True,
            http_code=int(raw.get("http_code", 200)),
            h5_pct=h5_pct,
            w7_pct=w7_pct,
            h5_reset_secs=h5_secs,
            w7_reset_secs=w7_secs,
            w7_opus_pct=w7_opus_pct,
            w7_scoped=tuple(scoped),
        )

    def save(self, name: str, result: ProbeResult) -> None:
        if not result.ok:
            return
        self._paths.usage_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        scoped = _serialize_scoped(result.w7_scoped, now)
        if not scoped:
            # An empty result usually means the OAuth usage fetch failed (429),
            # not that the limits vanished — keep the last known entries until
            # their own reset elapses instead of clobbering them.
            prev = self._read_raw(name) or {}
            scoped = [
                [e[0], e[1], e[2], e[3] if len(e) == 4 else None]
                for e in prev.get("w7_scoped") or []
                if isinstance(e, list) and len(e) in (3, 4) and float(e[2]) > now
            ]
        payload = {
            "probed_at": now,
            "http_code": result.http_code,
            "h5_pct": result.h5_pct,
            "w7_pct": result.w7_pct,
            "w7_opus_pct": result.w7_opus_pct,
            "h5_reset_at": now + result.h5_reset_secs,
            "w7_reset_at": now + result.w7_reset_secs,
            "w7_scoped": scoped,
        }
        self._atomic_write(self._path_for(name), payload)
        self._append_history(name, now, result.h5_pct, result.w7_pct)

    def update_scoped(self, name: str, scoped: tuple[ScopedLimit, ...]) -> None:
        """Persist freshly fetched scoped limits without a full probe save.

        ``status`` probes fetch scoped limits live but (unlike ``run``) never
        save the probe result. Successful OAuth fetches are rare under the
        endpoint's aggressive rate-limiting, so discarding them leaves the
        backfill stale for hours. This rewrites only ``w7_scoped`` in the
        existing cache entry (creating a minimal one when absent), leaving
        ``probed_at`` and the usage history untouched.
        """
        if not scoped:
            return
        self._paths.usage_dir.mkdir(parents=True, exist_ok=True)
        raw = self._read_raw(name) or {}
        raw["w7_scoped"] = _serialize_scoped(scoped, time.time())
        self._atomic_write(self._path_for(name), raw)

    def _atomic_write(self, path: Path, payload: object) -> None:
        # Cache files hold no secrets and churn on every probe: 0o644, compact.
        atomic_write_json(path, payload, mode=0o644, indent=None)

    def _load_history(self, name: str) -> list[list[float | None]]:
        try:
            raw = json.loads(self._history_path(name).read_text())
        except (json.JSONDecodeError, OSError):
            return []
        return raw if isinstance(raw, list) else []

    def _append_history(
        self, name: str, ts: float, h5_pct: float | None, w7_pct: float | None
    ) -> None:
        """Append one usage sample and prune to the retention window / point cap."""
        history = self._load_history(name)
        history.append([ts, h5_pct, w7_pct])
        cutoff = ts - USAGE_HISTORY_RETENTION_SECONDS
        history = [e for e in history if e and e[0] is not None and e[0] >= cutoff]
        history = history[-USAGE_HISTORY_MAX_POINTS:]
        self._atomic_write(self._history_path(name), history)

    def recent_rate(
        self,
        name: str,
        pct_now: float | None,
        *,
        window: str,
        now: float,
        tail_secs: int,
        min_span: int,
    ) -> float | None:
        """Observed burn in %-points/sec over the most recent tail span, or ``None``.

        Picks the oldest stored sample that is at least ``min_span`` but at most
        ``tail_secs`` old — the longest robust span inside the tail window — and divides
        the pct delta by the elapsed time. Returns ``None`` when there is no usable sample,
        ``pct_now`` is unknown, or usage dropped (a window reset fell inside the span, so
        the tail rate is meaningless and the caller falls back to the average pace).
        """
        if pct_now is None:
            return None
        col = _WINDOW_COL[window]
        best_ts: float | None = None
        best_pct = 0.0
        for entry in self._load_history(name):
            if len(entry) <= col:
                continue
            ts, then = entry[0], entry[col]
            if ts is None or then is None:
                continue
            age = now - ts
            if age < min_span or age > tail_secs:
                continue
            if best_ts is None or ts < best_ts:
                best_ts, best_pct = ts, then
        if best_ts is None:
            return None
        if pct_now < best_pct:  # window reset between samples → tail unreliable
            return None
        return (pct_now - best_pct) / (now - best_ts)
