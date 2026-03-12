# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## Self Evolution Commands

- `/self-update <instruction>`: apply code-level self changes in the current workspace.
- `/self-rollback <commit_sha>`: rollback code to a historical commit, then run compile and test gates before restarting the session worker.

Self-evolution guardrails:
- Workspace must be a git repository and contain source files.
- Working tree must be clean before executing self-update or self-rollback.
- Rollback target must be an ancestor commit of current HEAD.

`/self-rollback` result includes:
- `Rollback before HEAD`
- `Rollback target commit`
- `Rollback after HEAD`
- `Compile duration`
- `Test duration`
