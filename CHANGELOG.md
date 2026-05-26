# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-05-26

### Fixed

- **Session isolation: accounts no longer get logged out repeatedly.** Two
  bugs in the isolation token-sync path could revoke an account's tokens
  server-side and force a relogin:
  - `reconcile_isolated` copied a per-account `.credentials.json` back into
    `accounts.json` with no recency check, so a stale leftover file could roll
    a freshly refreshed token back onto an already-rotated (dead) one. It now
    only adopts a file written *after* the stored token's `obtained_at`.
  - The `accounts.json` flock covered only the final write, so a cron tick and
    a `run` could each spend the same rotating refresh token and trip
    Anthropic's refresh-token-reuse detection (which revokes the whole family).
    Refresh now holds the lock across the entire load → refresh → save and
    re-checks staleness under it.

## [0.1.0]

- Initial public release.
