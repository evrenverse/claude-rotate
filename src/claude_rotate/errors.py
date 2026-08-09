"""Typed exceptions for claude-rotate."""

from __future__ import annotations


class ClaudeRotateError(Exception):
    """Base for all errors we raise. CLI catches this and prints cleanly."""


class ConfigError(ClaudeRotateError):
    """Config file missing, malformed, or unreadable."""


class AccountError(ClaudeRotateError):
    """Account not found, duplicate, or invalid."""


class TokenFormatError(AccountError):
    """Pasted token fails local format validation."""


class LockTimeoutError(ClaudeRotateError):
    """Another writer held the accounts.json lock for too long."""


class ClaudeBinaryError(ClaudeRotateError):
    """The `claude` binary is missing, unresolvable, or too old."""
