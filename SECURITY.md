# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately by email to the address in
`pyproject.toml` under `Homepage` → GitHub issue with the "security"
template, or open a GitHub Security Advisory on the repo. Do **not** file a
public issue for anything that could disclose a live token.

We'll triage within one week. Fixes that land in `main` get an immediate patch
release.

## Scope

This project stores OAuth tokens for Anthropic's Claude subscriptions. In
scope:

- Token leakage from any path claude-rotate writes to disk, logs, or stdout/
  stderr (including error messages).
- Race conditions around reading/writing `accounts.json` or the lock file.
- Symlink / TOCTOU attacks against the config / cache / state directories.
- Any escalation from "can run claude-rotate as the user" to "can read
  another user's tokens" on multi-tenant systems.

Out of scope:

- Anthropic's own token rotation, refresh-endpoint semantics, or upstream
  claude binary behaviour — report those directly to Anthropic.
- The contents of `~/.claude/.credentials.json`, which claude itself
  manages.

## Handling tokens safely

If you're writing a PR, a bug report, or sharing logs:

- **Never paste a full `sk-ant-*` token** into an issue, PR, CI log, or
  transcript. Redact to the first 20 characters (``sk-ant-oat01-xxxx…``) or
  replace entirely.
- When reproducing a bug, use `claude-rotate cleanup` to wipe state first
  and run `doctor` / `status` against fresh tokens in an isolated
  `CLAUDE_ROTATE_DIR=/tmp/…`.
- The file `~/.config/claude-rotate/accounts.json` is `chmod 600`; its
  parent directory is `chmod 700`. Preserve those modes if you hand-edit.

## Threat model, briefly

claude-rotate trusts:

- The user running it (local file permissions, shell alias, `$PATH`).
- Anthropic's TLS endpoints (`api.anthropic.com`, `platform.claude.com`).
- Python's `tempfile.mkstemp` + `os.replace` for atomic writes.

claude-rotate does **not** trust:

- Other users on the same machine (file modes enforced on the config dir).
- The browser / callback server after the redirect completes (`state`
  parameter is verified, PKCE `code_verifier` is required).
- Symbolic links inside its managed directories (`cleanup` refuses to
  recurse through them).
