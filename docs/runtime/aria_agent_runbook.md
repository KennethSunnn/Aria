# ARIA Agent Runbook (Ops First)

This runbook standardizes how to invoke and validate multi-agent execution in ARIA.

## Role checklist (human or external agent)

Use these hats when planning multi-step work; they are not loaded from IDE config files.

- Orchestration: plan phases, minimal specialists, explicit handoffs
- DevOps / infra: automation, rollback, observability
- Incident response: severity, containment, comms cadence
- API / performance testing: deterministic assertions, baselines
- Reality gate: evidence over claims before sign-off

## Quick Start

1. Pick a template from `docs/runtime/nexus_micro_ops_templates.md`.
2. Fill scope/impact/target fields.
3. Execute phased plan with an explicit orchestration owner.
4. Require a final PASS / NEEDS WORK gate with cited evidence.

## Ops Scenarios

- **Incident handling**: commander -> infra -> reality gate
- **Release automation**: devops -> api tester -> reality gate
- **Performance stabilization**: benchmarker -> infra -> devops

## Escalation Policy

- Max 3 retries per failing step.
- If still failing, mark `blocked` and escalate with:
  - failing step
  - evidence
  - attempted mitigations
  - owner and ETA

## Acceptance Checklist

- At least one explicit PASS/FAIL gate result exists.
- Rollback path is defined for infra/release changes.
- Evidence includes deterministic checks (tests/status/metrics).
- Final output includes unresolved risks and next actions.

## Memory Usage Checklist

- At start: `recall` with `project + agent + task topic`.
- At key milestone: `remember` checkpoint with tags.
- Before handoff: `remember` pending work and constraints.
- On regression: `search` last known-good, then `rollback`.

