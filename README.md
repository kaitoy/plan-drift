# plan-drift

A Claude Code plugin that uses TypeSafe's System One model [Jev](https://docs.typesafe.ai/) to check whether the implementation diff matches the plan you approved in Claude Code's Plan mode.

## How it works

| When | Hook | What it does |
|---|---|---|
| Prompt submitted in Plan mode | UserPromptSubmit | Tells Claude to end the plan with a `plan-checks` block |
| Right before ExitPlanMode | PreToolUse | Denies the call if `plan-checks` is missing, so Claude rewrites the plan |
| After the plan is approved | PostToolUse | Saves the checks and a snapshot of the working tree (a git tree including untracked files) to `.claude/plan-drift/current.json` |
| End of each response | Stop | If the diff has changed, verifies it with Jev and shows a one-line summary (never blocks) |
| `/plan-drift:check` | Skill | Shows the full report and saves it to `.claude/plan-drift/report.md`, then Claude classifies each drift item (unintended / intentional / Jev misjudged) and records it with `plan_drift.py triage` |
| `/plan-drift:stats` | Skill | Aggregates every plan's latest result into `.claude/plan-drift/stats.html`: plan items vs. drift detected, drift rate, adjusted drift rate and Jev misjudged rate per plan, with charts |

Questions sent to Jev:
- **Per plan item**: state = `{plan_item, diff of the related files}`. `implemented` (Score 0–2) and `contradicts` (Noul)
- **Per changed file**: state = `{diff}`. `covered_by` (Choice: each plan item + `unplanned`) finds changes outside the plan
- Files named in the plan but never touched are reported by code, without Jev

## History

Every verification appends to `.claude/plan-drift/history.jsonl` (one `eval` record per check, one `triage` record per classification). Stats use the latest `eval` of each plan, so the Stop hook's repeated checks are counted once.

- drift = non-OK plan items + unplanned files; drift rate = drift / plan items
- adjusted drift rate = (drift − drift items classified `misjudged`) / plan items
- Jev misjudged rate = items classified `misjudged` / classified items

## plan-checks format

````
```plan-checks
- [C1] src/api/client.py :: fetch() retries with exponential backoff, at most 3 attempts
- [C2] src/api/client.py, tests/test_client.py :: RetryError is raised after the last attempt and a test covers it
- [C3] - :: No public function signature in src/api/ changes
```
````

`files` is a path relative to the repository root, a directory, or a glob. `-` means the whole diff. Write "must not change" constraints with `-`.

## Setup

Requirements: Python 3.10+ (standard library only), git, and a TypeSafe API key.

```sh
export TYPESAFE_API_KEY=sk-...        # https://console.typesafe.ai/keys
claude plugin marketplace add kaitoy/cc-plan-drift   # or a local path
claude plugin install plan-drift@cc-plan-drift
```

To just try it locally, `claude --plugin-dir /path/to/cc-plan-drift` also works.

- Hooks are launched with the `python` command. If your environment only has `python3`, edit hooks/hooks.json
- The model is pinned to `jev-1.13.0` (the thresholds are tuned for it). Set `PLAN_DRIFT_MODEL` to change it
- The decision thresholds are constants at the top of `scripts/plan_drift.py`

## Tests

```sh
python tests/test_plan_drift.py
```
