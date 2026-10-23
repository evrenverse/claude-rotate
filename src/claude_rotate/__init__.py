"""claude-rotate: quota-aware account rotator for Claude Code subscriptions."""

from importlib.metadata import PackageNotFoundError, version

# Read from the installed package, so a release bump in pyproject.toml is the only place to change.
try:
    __version__ = version("claude-rotate")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0"
