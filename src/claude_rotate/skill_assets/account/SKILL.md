---
name: account
description: Use when the user asks which account is active or current, account or subscription limits, quota or rate-limit usage, the 5-hour or weekly window, when the next claude launch will rotate accounts, remaining subscription days, or wants a claude-rotate account overview.
metadata:
  short-description: Show the active account and quota limits via claude-rotate
---

# Account Status (claude-rotate)

Reports which account **this agent session** is running on and the quota/limits
of every configured account, via `claude-rotate`.

## How to run

Run the command and show its output to the user **verbatim** — it is already
formatted (a status line, a bordered table, and warnings). Do not reformat,
re-wrap the table, summarize, or add commentary.

```bash
claude-rotate status --report
```

If `claude-rotate` is not installed or reports no accounts, relay that message
as-is. Never fabricate numbers.

## What it reports

- **Markers**: `@` = the account this session runs on; `>` = the account the
  rotator would pick next; `@>` = both (no rotation on the next launch).
- **One table, all accounts**: 5h % and reset, weekly % and reset, and days
  left on the subscription. Reset cells show the absolute clock (plus a weekday
  when it lands on another day) and the relative time, e.g. `Sun 09:00 (2d 8h)`.
- **Warnings**: weekly usage ≥ 90 %, forecast > 100 %, subscription expiring in
  under 7 days, accounts needing re-login, plus the freest fallback account.

## Maintenance

This skill is a thin wrapper; all logic lives in `claude-rotate` itself
(`claude_rotate.report.build_report`, exercised by its test suite). Update the
tool to change the output. Reinstall the skill with `claude-rotate
install-skill`.
