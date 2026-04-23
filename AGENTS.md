# Setup via AI agent

If you already use an AI coding agent (Claude Code, Cursor, Aider, Codex, Continue, etc.) you can hand it this file — or just the prompt block below — and it will set up `claude-rotate` end-to-end without you having to copy commands.

**If you are the agent reading this:** follow the steps in order, stop at every "PAUSE" marker and let the human act before you continue. Never fabricate token values, never skip the verification step.

---

## Copy-paste prompt

```text
Please set up `claude-rotate` on this machine for me. It is a quota-aware
rotator for multiple Anthropic Claude Code subscriptions. Follow the
instructions in claude-rotate's AGENTS.md exactly. Specifically:

1. Verify the real `claude` binary (v2.1.117+) is on PATH; if not, direct
   me to https://claude.com/claude-code and stop.
2. Install `claude-rotate` from GitHub. Prefer uv; fall back to pipx:
     uv tool install git+https://github.com/codename-cn/claude-rotate
   (claude-rotate is not on PyPI yet — use the GitHub URL.)
3. Add `alias claude='claude-rotate run'` to my shell rc (zsh or bash —
   detect which I use). Do NOT use `alias claude='claude-rotate'` — that
   would shadow upstream commands like `claude doctor`.
4. Reload my shell or `source` the rc.
5. Run `claude-rotate doctor` and show me the output.
6. For every subscription I want to add, run the interactive OAuth login:
     claude-rotate login <email> [<handle>]
   This opens a browser tab, runs the OAuth PKCE flow, captures the
   callback on localhost, and saves the tokens to
   ~/.config/claude-rotate/accounts.json. It will also prompt for a
   manual subscription-end date — only fill it in if claude.ai shows a
   pending cancellation for that account; otherwise press Enter to skip.
7. Install the background sync so in-session token rotations are kept
   authoritative in accounts.json:
     claude-rotate install-sync
   Confirm with:  crontab -l | grep claude-rotate
8. Run `claude-rotate list` and `claude-rotate status` — report back.
9. If anything errors, stop and show me the full error; do not retry
   blindly.

Never write a token value into a config file, shell history, git, or chat
transcript.
```

---

## Why the alias is scoped to `run`

`alias claude='claude-rotate run'` — not `alias claude='claude-rotate'` — is deliberate. With the narrower alias, any argument you pass to `claude` (e.g. `claude doctor`, `claude auth login`, `claude --version`) flows straight through to the real `claude` binary. Rotator-specific commands (`login`, `status`, `pin`, `set-expiry`, `install-sync`, `cleanup`, …) stay reachable only via explicit `claude-rotate <command>`, so they can never shadow an upstream command — present or future.

## What the agent must NOT do

- Do not paste token values into the chat / transcript / issue / PR.
- Do not commit `~/.config/claude-rotate/accounts.json` or `~/.claude/.credentials.json` anywhere.
- Do not run `claude-rotate cleanup` without explicit human confirmation — it wipes the local token store.
- Do not guess cancellation dates. If the user has a pending cancel on claude.ai and can't tell you the date, skip `set-expiry` — the dashboard will fall back to the next billing anchor.
- Do not uninstall the sync cron (`install-sync --uninstall`) without confirmation — removing it lets tokens drift between `.credentials.json` and `accounts.json` during long sessions.

## Verification checklist

After setup the agent should confirm each of these:

- [ ] `which claude-rotate` prints a path in `$PATH`.
- [ ] `claude-rotate --version` prints a version number.
- [ ] `type claude` confirms the alias expands to `claude-rotate run`.
- [ ] `claude-rotate doctor` reports all checks green (or explains each warning).
- [ ] `claude-rotate list` shows every logged-in account.
- [ ] `claude-rotate status` shows live quota bars for every account.
- [ ] `crontab -l | grep -q '\[claude-rotate:sync\]'` confirms the 2-minute sync job is installed.

If any of these fail, report the exact output and stop — do not try to "fix" it by guessing.
