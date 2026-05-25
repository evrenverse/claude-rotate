"""`claude-rotate config get|set` — read/write feature toggles in config.json."""
from __future__ import annotations

import sys

from claude_rotate.config import Paths
from claude_rotate.settings import RotateConfig, load_config, set_value


def _as_dict(cfg: RotateConfig) -> dict[str, object]:
    return {
        "session_isolation": cfg.session_isolation,
        "auto_resume.enabled": cfg.auto_resume_enabled,
        "auto_resume.message": cfg.auto_resume_message,
    }


def execute(paths: Paths, action: str, key: str | None, value: str | None) -> int:
    if action == "get":
        cfg = load_config(paths)
        data = _as_dict(cfg)
        if key is None:
            for k, v in data.items():
                print(f"{k} = {_fmt(v)}")
            return 0
        if key not in data:
            print(f"error: unknown config key {key!r}", file=sys.stderr)
            return 1
        print(_fmt(data[key]))
        return 0
    if action == "set":
        if key is None or value is None:
            print("usage: claude-rotate config set <key> <value>", file=sys.stderr)
            return 2
        set_value(paths, key, value)
        print(f"{key} = {value}")
        return 0
    print(f"error: unknown config action {action!r}", file=sys.stderr)
    return 2


def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
