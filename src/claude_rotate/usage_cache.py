"""On-disk cache of the last successful rate-limit probe per account.

Used as a fallback when a live probe fails (429, timeout, 5xx). The cache
remembers absolute reset timestamps so we can extrapolate seconds-remaining
at read time. Entries older than MAX_CACHE_AGE are ignored.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from claude_rotate.config import (
    USAGE_HISTORY_MAX_POINTS,
    USAGE_HISTORY_RETENTION_SECONDS,
    Paths,
)
from claude_rotate.probe import ProbeResult

MAX_CACHE_AGE_SECONDS = 10 * 60

# History sample layout: [probed_at, h5_pct, w7_pct]. The pct columns map to the
# ``window`` argument of ``recent_rate`` ("5h" -> index 1, "7d" -> index 2).
_WINDOW_COL = {"5h": 1, "7d": 2}


class UsageCache:
    def __init__(self, paths: Paths) -> None:
        self._paths = paths

    def _path_for(self, name: str) -> Path:
        return self._paths.usage_dir / f"{name}.json"

    def _history_path(self, name: str) -> Path:
        return self._paths.usage_dir / f"{name}.history.json"

    def load(self, name: str) -> ProbeResult | None:
        path = self._path_for(name)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
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

        return ProbeResult(
            ok=True,
            http_code=int(raw.get("http_code", 200)),
            h5_pct=h5_pct,
            w7_pct=w7_pct,
            h5_reset_secs=h5_secs,
            w7_reset_secs=w7_secs,
            w7_opus_pct=w7_opus_pct,
        )

    def save(self, name: str, result: ProbeResult) -> None:
        if not result.ok:
            return
        self._paths.usage_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        payload = {
            "probed_at": now,
            "http_code": result.http_code,
            "h5_pct": result.h5_pct,
            "w7_pct": result.w7_pct,
            "w7_opus_pct": result.w7_opus_pct,
            "h5_reset_at": now + result.h5_reset_secs,
            "w7_reset_at": now + result.w7_reset_secs,
        }
        self._atomic_write(self._path_for(name), payload)
        self._append_history(name, now, result.h5_pct, result.w7_pct)

    def _atomic_write(self, path: Path, payload: object) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self._paths.usage_dir), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, str(path))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

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
