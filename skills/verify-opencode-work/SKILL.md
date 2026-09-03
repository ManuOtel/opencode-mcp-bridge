---
name: verify-opencode-work
description: Independently verify a finished opencode worker task via diffs, tests, and evidence before accepting it.
---

# Verify Opencode Work

Use when a worker reports `idle`. Never trust only the worker summary.

## Inspect, do not assume

- Fetch the diff yourself (`get_diff` with the worker `taskID`, optional `messageID`, and the worker `directory`).
- Read the changed files in the repo. Check scope: only intended files touched, no secrets, no stray artifacts.
- Run proportionate checks: at minimum the repo's fast tests/lint for the touched area; full suite before merge. See `AGENTS.md` for commands.
- Confirm acceptance criteria from `delegate-to-opencode` one by one. Missing criteria means the task is not done.

## Use worker_verify when available

- `worker_verify` is a read-only helper: it re-checks state and evidence without side effects. It never replaces reading the diff and running checks.
- Treat `error` state or an error flag in the latest output as failure even if the text sounds confident.

## Report

- Done: list files changed, checks run with results, and remaining follow-ups.
- Not done: say exactly which criterion failed, with file/line or command output as evidence, then re-scope or retry.
