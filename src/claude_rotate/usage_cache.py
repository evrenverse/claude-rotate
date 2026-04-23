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

from claude_rotate.config import Paths
from claude_rotate.probe import ProbeResult

MAX_CACHE_AGE_SECONDS = 10 * 60


class UsageCache:
    def __init__(self, paths: Paths) -> None:
        self._paths = paths

    def _path_for(self, name: str) -> Path:
        return self._paths.usage_dir / f"{name}.json"

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
        if h5_pct is not None and h5_secs == 0:
            h5_pct = 0.0
        if w7_pct is not None and w7_secs == 0:
            w7_pct = 0.0

        return ProbeResult(
            ok=True,
            http_code=int(raw.get("http_code", 200)),
            h5_pct=h5_pct,
            w7_pct=w7_pct,
            h5_reset_secs=h5_secs,
            w7_reset_secs=w7_secs,
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
            "h5_reset_at": now + result.h5_reset_secs,
            "w7_reset_at": now + result.w7_reset_secs,
        }
        fd, tmp = tempfile.mkstemp(dir=str(self._paths.usage_dir), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, str(self._path_for(name)))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
