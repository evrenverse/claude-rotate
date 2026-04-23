from __future__ import annotations

import json
from pathlib import Path

from claude_rotate.config import Paths
from claude_rotate.state_log import StateLog


def make_paths(tmp_path: Path) -> Paths:
    return Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=tmp_path / "state",
    )


def test_state_log_appends_jsonl(tmp_path: Path) -> None:
    log = StateLog(make_paths(tmp_path))
    log.event("probe", account="main", http_code=200, request_id="req_abc")
    log.event("exec", chosen="main", args=["claude", "hi"])
    lines = (tmp_path / "state" / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    e2 = json.loads(lines[1])
    assert e1["event"] == "probe"
    assert e1["account"] == "main"
    assert e2["event"] == "exec"
    assert "ts" in e1  # timestamp is always added


def test_state_log_swallows_write_errors(tmp_path: Path) -> None:
    # Path that cannot be created (file-in-path collision)
    bad = tmp_path / "not-a-dir"
    bad.write_text("file, not dir")
    paths = Paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
        state_dir=bad,
    )
    log = StateLog(paths)
    log.event("probe", account="main")  # must not raise
