---
name: stats
description: Aggregates plan-drift results over all plans and sessions in this repository (plan size vs. drift detected, Jev misjudged rate) into an HTML report with charts. Use when you want to see plan drift trends or metrics across sessions.
allowed-tools: Bash(python:*)
---
!`python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_drift.py" stats`

Above is the output of plan-drift stats. Tell the user the summary line and the path of the HTML report, nothing more.
