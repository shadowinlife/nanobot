---
name: self-evolution-git
description: Required workflow for nanobot self code evolution. Use git-managed changes, enforce tests, and produce searchable rollback-friendly commits.
metadata: {"nanobot":{"always":true}}
---

# Self Evolution (Git Guardrails)

Use this workflow whenever the task changes nanobot's own code-level capability, especially:
- adding or changing tools
- changing agent loop, routing, context, prompt assembly, or skill loading
- changing provider/channel/gateway behavior

## Mandatory OpenCode Route

For any self-evolution or `/self-update` coding mutation, OpenCode ultrawork must be the execution path.

- Required: use OpenCode with `ulw <task>` for implementation work.
- Required: provide OpenCode with explicit scope, constraints, and validation commands.
- Forbidden: bypassing OpenCode and performing pure manual dynamic edits for self-evolution.
- If OpenCode is unavailable, stop mutation and report a blocked precondition.

## Preconditions (must pass before editing)

1. Workspace must be a git repository (`.git` exists).
2. Workspace must contain source files (not an empty/non-source folder).
3. If either check fails, do not perform code mutation. Report the precondition failure.

## Required Change Workflow

1. Inspect first
- Read current implementation and related tests before editing.
- Identify impacted modules and tests.

2. Create/update tests first
- Add or update unit tests to capture expected behavior and regressions.
- Include rollback/failure-path tests when behavior can fail.

3. Implement minimal code change
- Keep patch scope minimal and directly tied to test intent.
- Avoid unrelated refactors in the same commit.
- Execute implementation through OpenCode ULW for self-evolution tasks.

4. Validate quality gates (required)
- Compile/syntax must pass for changed source files.
- All unit tests must pass.
- If full-suite runtime is large, run targeted tests first, then full suite before finalizing.

5. Commit with searchable rollback-friendly message (required)
- Commit after tests pass.
- Message format:
  - `type(scope): concise summary`
  - blank line
  - `Why: ...`
  - `What: ...`
  - `Validation: compile=<pass/fail>, tests=<command + result>`
- Include key symbols/files in summary or body to improve retrieval.

## Commit Message Examples

```text
feat(agent-tools): integrate opencode tool for self-update coding flow

Why: self-update should use OpenCode ULW for coding execution and staged planning
What: add OpenCodeTool and route code-writing workflow through `opencode`
Validation: compile=pass, tests=pytest -q tests/test_opencode_tool.py => pass
```

```text
fix(self-update): rollback invalid python edits and restart worker

Why: self-mutation must fail safe with automatic recovery
What: add snapshot/validate/rollback flow and /self-update guardrails
Validation: compile=pass, tests=pytest -q tests/test_self_update_manager.py => pass
```

## Hard Rules

- No code-level self capability change without git-tracked commit.
- No finalization when compile or tests fail.
- No vague commit subjects like `update` or `fix stuff`.
- Each commit must be independently understandable and revertable.

## Runtime Enforcement

For `/self-update` flows, nanobot runtime enforces these gates before applying changes:
- workspace must be a git repository with source files
- git working tree must be clean before mutation
- changed Python files must compile
- full unit test gate (`pytest -q`) must pass
- changes must be committed with a structured Why/What/Validation message

If any gate fails, changes are rolled back automatically.
