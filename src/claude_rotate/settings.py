"""Feature configuration for claude-rotate (config.json).

Two opt-in toggles, both OFF by default. A missing or corrupt config.json
yields defaults, so installs that never configure anything keep the exact
pre-feature behaviour.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from claude_rotate.config import Paths
from claude_rotate.errors import ConfigError

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


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}
_BOOL_KEYS = {"session_isolation", "auto_resume.enabled"}


def save_config(paths: Paths, cfg: RotateConfig) -> None:
    path = paths.config_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = {
        "session_isolation": cfg.session_isolation,
        "auto_resume": {
            "enabled": cfg.auto_resume_enabled,
            "message": cfg.auto_resume_message,
        },
    }
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=".config.json.tmp-")
    tmp = Path(tmp_str)
    try:
        tmp.chmod(0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _coerce_bool(value: str) -> bool:
    low = value.strip().lower()
    if low in _TRUTHY:
        return True
    if low in _FALSY:
        return False
    raise ConfigError(f"expected a boolean (true/false), got {value!r}")


def set_value(paths: Paths, key: str, value: str) -> RotateConfig:
    cfg = load_config(paths)
    if key == "session_isolation":
        cfg = RotateConfig(
            session_isolation=_coerce_bool(value),
            auto_resume_enabled=cfg.auto_resume_enabled,
            auto_resume_message=cfg.auto_resume_message,
        )
    elif key == "auto_resume.enabled":
        cfg = RotateConfig(
            session_isolation=cfg.session_isolation,
            auto_resume_enabled=_coerce_bool(value),
            auto_resume_message=cfg.auto_resume_message,
        )
    elif key == "auto_resume.message":
        cfg = RotateConfig(
            session_isolation=cfg.session_isolation,
            auto_resume_enabled=cfg.auto_resume_enabled,
            auto_resume_message=value,
        )
    else:
        raise ConfigError(
            f"unknown config key {key!r}; valid keys: "
            "session_isolation, auto_resume.enabled, auto_resume.message"
        )
    save_config(paths, cfg)
    return cfg
