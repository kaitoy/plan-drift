---
name: check
description: Verifies with Jev how the implementation diff drifts from the plan approved in Plan mode, and reports it. Use when you want to confirm "was it implemented as planned" or check for "plan drift".
allowed-tools: Bash(python:*)
---
!`python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_drift.py" check`

Above is the output of plan-drift. If verification ran, the report is also saved to `.claude/plan-drift/report.md`.
For each ❌ / ⚠️ item and each unplanned change, check the actual diff and classify it into one of:
1. **Unintended drift** — a real deviation you did not mean to make (missed work, stray change, etc.).
2. **Intentional, already reported** — a real deviation you made on purpose AND explained to the user earlier in this conversation. Quote or point to where you told them.
3. **Jev misjudged** — the diff actually matches the plan.
Only use category 2 if you can find the explanation in this conversation; if you can't, put it in category 1.
Report category 1 first, grouped by category, one short line per item. Do not modify code until the user asks you to.

Then record your classification with the `triage` command printed at the end of the output (one Bash call, all items at once), so it shows up in `/plan-drift:stats`. Use the check ID for plan items and `file:PATH` for unplanned files, e.g. `C2=unintended C3=misjudged file:src/x.py=intentional`.
