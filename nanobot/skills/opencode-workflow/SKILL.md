---
name: opencode-workflow
description: Always-on workflow for coding tasks with OpenCode ultrawork. Decompose work into programmable sub-tasks and prefer scripts for repetitive operations.
metadata: {"nanobot":{"always":true}}
---

# OpenCode Programming Workflow

Use this workflow for coding tasks, especially when tasks involve multi-file edits, repeated operations, or long-running implementation work.

## Core Goals

1. Decompose work into programmable units
- Split requests into concrete, testable sub-tasks (analysis, implementation, validation, integration).
- Express each sub-task with explicit inputs, outputs, and acceptance checks.
- Prioritize deterministic steps that can be run by scripts/commands.

2. Prefer scripts for repeated work
- If an operation repeats 2 or more times, first consider writing a script to perform it.
- Typical script-first cases:
  - repetitive search/replace with guardrails
  - scaffold generation across many files
  - batch validation, lint, formatting, or migration checks
  - repetitive refactor patterns with the same AST/text transform
- Keep scripts small, idempotent when possible, and committed with the related change.

## OpenCode ULW Execution Rules

When OpenCode is available, use OpenCode ultrawork mode to execute implementation-heavy coding tasks.

- Use prompt prefix: `ulw <task>`
- Prefer structured tasks with:
  - objective
  - constraints
  - target files/modules
  - validation commands
- Require machine-verifiable completion (tests, build, lint, or syntax checks).

## Task Decomposition Template

Use this structure before execution:

```text
Goal:
Constraints:
Subtasks:
1) ...
2) ...
3) ...
Validation:
- command A
- command B
Definition of Done:
```

## Script-First Decision Policy

Before starting implementation, evaluate:

- Can this be expressed as a deterministic script?
- Will this operation repeat?
- Is manual LLM-driven editing likely to be slower or less reliable?

If yes, create and run a script first, then let LLM handle only residual non-deterministic edits.

## Failure Handling

- If OpenCode is unavailable, explicitly report that OpenCode execution is blocked.
- Do not silently switch to fully manual dynamic editing for tasks that are expected to run with ULW.
- For partial failures, reduce scope and retry with smaller sub-tasks.
