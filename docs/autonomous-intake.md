# Bounded semantic intake

`intake explore` turns rough, source-cited intent into grounded task actions
before implementation. It is an internal Go primitive for substantial or unclear
work, not a keyword classifier and not a second task queue.

```sh
python3 cli/go.py intake explore /path/to/repo \
  --intent "<exact request>" --source-ref "<durable origin>" \
  --authority planning \
  --executor-agent codex --model gpt-6-astra --effort high \
  --write --json
```

Without `--write`, the command only returns the versioned request and performs
no adapter call. With `--write`, assessment runs against a disposable copy of the
target repository. A custom `--assessment-command` is supported for deterministic
protocol tests; production model assessment uses the existing controlled Codex
adapter, a read-only critic sandbox, an explicit model/effort and bounded timeout.

The model receives `go-workflow.intake-request.v1` inside the standard
`GO_ADAPTER_REQUEST_JSON`. That request contains the exact intent text/hash/source,
separate `O#` outcomes, explicit advice/planning/execute authority, vision and
principle hashes, compact canonical task state and latest decision dispositions.
List items preserve coverage but do not force one task per line.

The final adapter result must include a schema-valid
`go-workflow.intake-assessment.v1`. Deterministic validation then proves only the
transport and safety contract:

- every action says `create`, `reuse` or `update` and links exact `O#` outcomes;
- substantial actions include observable before/after behavior, states, edges,
  non-goals, enforceable dependencies, delegated choices and matching proof;
- an unknown root cause can only create research, never a bug-fix claim;
- relevant decisions must match their current accepted/proposed/superseded/rejected
  state; the model cannot resurrect a superseded decision;
- user questions have owners and exact affected task IDs; independent work remains
  eligible;
- all actions validate before any task is written, so a later bad action cannot
  leave a partial intake.

This validation does not assert that the assessment is semantically good. The
actual model/reviewer supplies that judgment, and its structured assessment,
selection and runtime usage remain in `.go/intake/<intake-id>.json`. Repeating
the same intent/source/authority is idempotent and does not call the adapter or
create a suffixed task. Later feedback gets a new intake record and may update an
open task while retaining prior R# text, source, scope, acceptance and verification.

## Authority and questions

Advice and planning may persist assessment, questions and tasks, but they never
grant implementation authority. Manual claim and autonomous preflight reject an
intake task unless `authority.mode` is `execute` with
`implementation_authorized: true`. A task with unresolved question IDs is also
ineligible. Another execute-authorized task from the same assessment remains
claimable when no question blocks it.

The command never edits vision, architecture briefs or the decision ledger. A
material choice remains a question or existing decision reference until its
actual owner resolves it through the existing architecture/decision path. Task
release and deployment authority remain separate downstream gates.

## Compatibility boundary

Existing tasks, tiny fixes, research tasks and recommendation/execution-brief
flows keep their current representation. Only tasks materialized by this intake
primitive receive `intake`, `intake_history` and `task_design`. Legacy records do
not acquire new ceremony or inferred authority.

The JSON Schema for the adapter-owned assessment is
[`schemas/intake-assessment.schema.json`](../schemas/intake-assessment.schema.json).
Runtime validation additionally checks repository bindings, current decision
state, outcome/question references, idempotence and task lifecycle safety.
