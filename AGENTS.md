# ARIA Agent Runtime Rules

## Purpose
- This file is an index, not a handbook.
- Keep this file under 60 lines.
- When a rule is added, link it to a real failure mode.

## Source Of Truth
- Repository content is the only trusted source.
- Do not assume unwritten team conventions.
- Prefer `README.md` and module docstrings over guesswork.

## Hard Safety Boundaries
- Never claim file/app actions succeeded unless execution report says success.
- Never claim desktop/browser automation happened if runtime marks capability unavailable.
- For destructive actions (`file_delete`, `shell_run`, KB purge), require explicit confirmation path.

## Planning Rules
- Always set `task_form`: `local_execute`, `web_information`, `qa_only`, or `mixed`.
- Keep action list minimal; select only tools required for the task form.
- If required info is missing, return `clarify` with concrete choices.

## Execution Rules
- Prefer deterministic checks (exists/not exists/status) over narrative confidence.
- On failure, read `error_code` and `stderr` first, then decide retry/manual takeover.
- Avoid infinite retries; escalate when bounded retries are exhausted.

## Feedback Loop
- Every recurring mistake should produce one of:
  - A guardrail in code
  - A lint/test check
  - A clearer runtime message
- Benchmark and health reports must be used to prioritize fixes.

## Operational Hygiene
- Run regression benchmark before merging behavior changes.
- Keep docs, prompts, and constraints aligned with actual runtime behavior.
- Use weekly housekeeping to detect drift in docs/rules/quality.

## Action registry drift
- **Failure mode:** planner suggests tools that are blocked at runtime or skip safety metadata. When adding an `action` type, update `ALLOWED_ACTION_TYPES`, `USER_GATE_ACTION_TYPES` / `HIGH_RISK_ACTION_TYPES` if applicable, `action_registry`, and any permission read-only sets in `runtime/permissions.py`; extend `tests/test_action_registry_consistency.py` if you introduce intentional exceptions.
