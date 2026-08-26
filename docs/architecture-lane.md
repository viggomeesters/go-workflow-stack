# Conditional Architecture Lane

The architecture lane adds traceable architecture control to Go without making every task an architecture ceremony.

## Activation

The lane is opt-in and backward compatible:

- a repository without `.go/architecture/` and a task without `architecture` metadata behaves exactly as before;
- a task with explicit `architecture` metadata is governed immediately;
- once `.go/architecture/` exists, deterministic hard signals can require classification before claim;
- a deterministic minimum can raise impact, but an agent cannot lower it.

Impact levels:

| Impact | Meaning | Extra control |
|---|---|---|
| `none` | no architecture consequence | normal task lifecycle |
| `local` | local and readily reversible | principles plus normal critic |
| `material` | boundary, contract, data, integration, security, privacy, migration, or quality attribute changes | accepted brief and decisions, conformance evidence |
| `foundational` | source of truth, trust boundary, identity, ownership, irreversible platform or vendor direction | material controls plus named human approval |

## Canonical state

```text
.go/
  architecture-principles.json
  decisions/events.jsonl
  architecture/
    briefs/<scope-id>.json
    events.jsonl
```

- Principles remain durable project constraints.
- Decision trade-offs remain in the existing append-only decision ledger.
- Architecture briefs define a bounded scope, stakeholders, constraints, risks, and measurable quality attributes.
- Architecture events record classification, review, conformance, deviations, and time-bounded waivers.
- Normal verification evidence remains in `.go/evidence/events.jsonl`; conformance events reference it instead of creating a second test system.

Markdown and diagrams are human views, not workflow state. Add a diagram only when it answers a decision, risk, alignment, or handoff question.

## Task metadata

```json
{
  "architecture": {
    "impact": "material",
    "scope_refs": ["integration-boundary"],
    "concerns": ["integration", "security"],
    "decision_ids": ["integration-boundary-v1"],
    "conformance_required": true,
    "human_gate": "decision"
  }
}
```

Tasks contain references, not copied architecture prose. `GO_CONTEXT_JSON.applicable_architecture` resolves the exact briefs, decisions, quality attributes, open deviations, and active waivers. Exact referenced decisions remain available even when they are older than the recent-decision window.

## Operator flow

Classify before claim:

```bash
./go architecture classify . \
  --task-id <task-id> \
  --impact material \
  --scope-ref <scope-id> \
  --decision-id <decision-id> \
  --concern integration \
  --write --actor hermes --json
```

Inspect the applicable contract:

```bash
./go architecture validate . --json
./go architecture readback . --task-id <task-id> --json
./go architecture status . --json
```

A material or foundational claim fails closed when an applicable brief is missing or unaccepted, referenced decisions are missing or unaccepted, or no measurable quality attribute exists.

Record conformance after implementation and verification:

```bash
./go architecture conformance . \
  --task-id <task-id> \
  --scope-id <scope-id> \
  --status passed \
  --decision-check '<decision-id>=passed=.go/decisions/events.jsonl' \
  --quality-attribute-check '<attribute-id>=passed=<verification-command>' \
  --evidence-ref '.go/evidence/events.jsonl#<proof>'
```

Every referenced scope needs its own passing conformance event. Every referenced decision and quality attribute needs a passing or explicitly waived check.

## Human approval

`human_gate` can be `none`, `decision`, or `risk_acceptance`. Foundational impact always requires a human gate.

```bash
./go architecture review . \
  --task-id <task-id> \
  --scope-id <scope-id> \
  --status approved \
  --human \
  --actor '<named-human>' \
  --evidence-ref '<review-reference>'
```

The ledger records a declared named human and review evidence. It does not pretend local CLI text cryptographically proves identity or organizational authority. Automation identities such as `hermes`, `codex`, or `architecture-critic` cannot self-declare human approval.

## Deviations and waivers

A deviation is not green. Repair it or grant an explicit temporary waiver:

```bash
./go architecture waiver . \
  --id <waiver-id> \
  --task-id <task-id> \
  --scope-id <scope-id> \
  --reason '<why repair cannot happen now>' \
  --accepted-risk '<specific accepted consequence>' \
  --expires-at '2026-09-30T17:00:00+02:00' \
  --actor '<named-risk-owner>'
```

A waiver requires an actor, reason, accepted risk, and future timezone-aware expiry. Expired active waivers invalidate architecture state until an expiration/revocation event or repair closes them. Close the append-only lifecycle explicitly:

```bash
./go architecture waiver-close . \
  --id <waiver-id> \
  --task-id <task-id> \
  --scope-id <scope-id> \
  --status expired \
  --reason '<expired or risk repaired>' \
  --actor '<named-risk-owner>'
```

`passed_with_waiver` must reference an active waiver ID. Recording conformance with `--status deviation` writes an `architecture.deviation.recorded` event so status/readback cannot disguise it as an ordinary failed check.

## Migration

No bulk migration is required.

1. Existing repositories remain valid with no architecture folder and no task metadata.
2. Add an accepted brief only for the first genuinely material scope.
3. Classify new impact-bearing tasks explicitly.
4. Link existing accepted decisions instead of rewriting their history.
5. Record conformance from existing verification evidence.
6. Add human gates only where decision authority or risk acceptance actually requires them.

Do not backfill architecture theater across historical tasks. Start at the next consequential change.
