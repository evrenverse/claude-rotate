"""Feature configuration for claude-rotate (config.json).

Two opt-in toggles, both OFF by default. A missing or corrupt config.json
yields defaults, so installs that never configure anything keep the exact
pre-feature behaviour.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from claude_rotate.config import Paths

DEFAULT_RESUME_MESSAGE = "weiter gehts"


@dataclass(frozen=True)
class RotateConfig:
    session_isolation: bool = False
    auto_resume_enabled: bool = False
    auto_resume_message: str = DEFAULT_RESUME_MESSAGE


def load_config(paths: Paths) -> RotateConfig:
    path = paths.config_file
    if not path.exists():
        return RotateConfig()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return RotateConfig()
    if not isinstance(raw, dict):
        return RotateConfig()
    resume = raw.get("auto_resume") or {}
    if not isinstance(resume, dict):
        resume = {}
    return RotateConfig(
        session_isolation=bool(raw.get("session_isolation", False)),
        auto_resume_enabled=bool(resume.get("enabled", False)),
        auto_resume_message=str(resume.get("message", DEFAULT_RESUME_MESSAGE)),
    )
