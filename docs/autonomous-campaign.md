# Bounded serial campaign contracts

A campaign connects an authorized request to a bounded goal and existing task
outcomes. It is an opt-in **contract**, not another task queue, a runner, an
approval, or a completion record. This first increment supplies the versioned
schema, pure validation functions and a read-only CLI check. Exploration,
controller execution, cumulative accounting, recovery, goal auditing and
publication integration belong to the dependent autonomy tasks.

```sh
python3 cli/go.py validate /path/to/repo --campaign /path/to/contract.json --json
python3 cli/go.py validate /path/to/repo --campaign /path/to/revision-2.json \
  --previous-campaign /path/to/revision-1.json --json
```

The check reads the canonical `.go` even when invoked in an owned task worktree.
It writes nothing, launches no workers and performs no publication. Without
`--campaign`, existing validation and historical contracts retain their semantics.
The validator does not discover a campaign from a broad vision or an empty queue.
The controller chooses and stores immutable revisions; this increment does not
introduce an on-disk active-campaign pointer or a new state writer.

## Contract and traceability

[`campaign-contract.schema.json`](../schemas/campaign-contract.schema.json)
defines `go-workflow.campaign-contract.v1`. All fields are explicit; unknown
fields are rejected. Runtime validation additionally checks relationships and
canonical references that JSON Schema cannot prove.

| Field | Meaning and validation |
| --- | --- |
| `id`, `project`, `revision`, `previous_sha256` | Project-bound campaign identity; positive revision; revision 1 has no predecessor. Later revisions require the preceding contract, its digest and an increment of exactly one. |
| `intent` | Exact original request text, SHA-256 of its UTF-8 bytes, and source reference. All three remain unchanged across revisions. |
| `goal` | Adopted goal text, non-goals and stable outcome IDs. Refining the goal cannot erase or rewrite existing outcome promises or weaken their required evidence. |
| `goal.outcomes[].task_outcomes` | References to existing `task_id`/`requirement_id` R# pairs with `text_sha256` of the task outcome text. IDs, project/state and text hashes are checked against canonical tasks. No copied outcome statuses or evidence receipts. |
| `goal.outcomes[].required_evidence` | `verification` and `critic` are mandatory. Task release requirements add `release`; required deployment adds `live`. These are proof obligations, never proof that delivery happened. |
| `basis` | Byte hashes of `.go/vision.json` and `.go/architecture-principles.json`, plus exact governing decision IDs. Drift requires review and a new revision. Accepted decisions must still be accepted in the existing decision ledger. |
| `authority` | Cited authority, planning/execution mode, exact permitted task IDs, bounded expansion, explicit model/effort pairs, shared budget and separate release/deployment grants. |
| `decisions` | Decision references and dispositions, with owner, resolution gate, affected task IDs and bounds. The existing decision ledger remains the source of accepted governing decisions. |

`contract_digest` uses UTF-8 JSON with sorted keys, no ASCII escaping, compact
separators and no NaN. Whitespace in the contract file is immaterial to that
digest. Intent and task outcome hashes use their exact text; basis hashes use
exact file bytes. A revised original request starts a new campaign rather than
rewriting the initial request. New outcomes may be added in a new authorized
revision; old promises remain visible. Remapping a promise to different task R#
references requires grounded semantic review, even if the hashes are valid.

A minimal outcome entry is:

```json
{
  "id": "O1",
  "text": "The requested observable behavior works",
  "task_outcomes": [
    {
      "task_id": "existing-task",
      "requirement_id": "R1",
      "text_sha256": "<SHA-256 of that canonical R1 text>"
    }
  ],
  "required_evidence": ["verification", "critic", "release"]
}
```

This fragment is illustrative, not a complete executable contract. Executable
positive and negative fixtures live in
[`test_autonomy_contracts.py`](../tests/test_autonomy_contracts.py).

## Authority and decisions

`authority.mode` is `planning` or `execute`. Both require a nonempty
`source_ref`. Planning can retain unmapped outcomes and an empty task allowlist;
it grants no task execution, release or deployment. Execution requires every
adopted outcome to have an existing task mapping and every permitted task to
contribute to an adopted outcome. No wildcard task IDs are accepted.

Decision dispositions are deliberately distinct:

| Disposition | Meaning |
| --- | --- |
| `accepted` | Exact existing accepted decision in `.go/decisions/events.jsonl`. At least one accepted reference governs the campaign basis. |
| `delegated` | Routine bounded implementation choice citing the campaign's existing authority source, with explicit bounds and verification gate. It is not a substitute for an accepted governing architecture decision. |
| `proposed` | A candidate choice, with owner and resolution gate. Affected tasks remain ineligible. |
| `unresolved` | An open material choice, with owner and resolution gate. Affected tasks remain ineligible. |

`campaign_task_findings(contract, task)` checks static campaign restrictions:
planning mode, exact task membership, unresolved/proposed decisions affecting
that task, and explicit task/critic model profiles. An independent task remains
eligible under these restrictions when another task has an unresolved choice.

A valid contract can describe blocked work. Callers must first validate the
contract against the repository with `campaign_findings`, then check task
restrictions, and still apply existing design review, architecture,
dependency, workspace, model availability, budget and completion gates. An empty
findings list is not a readiness verdict, semantic evidence or user permission.
No new human approval system is introduced; existing named-human architecture
gates continue to apply when required. Source strings identify evidence for
review; the validator does not authenticate a user's words or an agent's claim
of delegated authority.

## Expansion, budgets and delivery bounds

`authority.expansion` contains separate `research` and `repair` allowances. Each
has `max_tasks`, repository-relative `modify` patterns and adopted `outcome_ids`.
Zero disables that allowance and requires empty bounds. Positive limits require
both a scope and outcome links. Absolute, parent-traversing and platform drive
paths are rejected. The allowance is an upper bound: a future controller must
check the proposed task's actual scope and goal fit, count admissions across
revisions, persist provenance, and admit it in a new contract revision **before**
execution. An unlisted task never becomes executable just because expansion is
allowed. Exhausting the queue never authorizes unrelated work from the vision.

`authority.models` lists exact model IDs and effort values. There is no default
model, silent fallback or automatic escalation. Execute contracts check each
task's explicit model and optional critic model against that list. Existing
runtime capability/effective-identity checks remain necessary before dispatch.

`authority.budget` has positive integer `wall_seconds`, `max_tasks` and
`max_attempts`. These are campaign-wide ceilings covering initial work,
research, repair and retries across interruption/resume, not fresh per-task
allowances. The controller must retain consumption; rewriting a contract cannot
reset it. A contract can permit more tasks than can fit the current budget.
Budget exhaustion stops work rather than making the contract structurally
invalid. Metering and enforcement are intentionally not claimed by this build.

`authority.release` names configured project release profiles and separately
sets `allow_push`; an enabled grant requires its own source reference.
`authority.deployment` lists configured target identities and its own source
reference. Push authority never implies deployment authority. Empty lists,
`allow_push: false`, and null references deny those grants. Requiring release or
live evidence for a task does not authorize producing it: missing authority
keeps delivery pending for the existing publisher. Configured profiles and
existing publication/deployment safety and readback gates remain authoritative.

All six stop conditions must be declared:

- `goal_verified`: stop only after actual goal evidence passes the goal audit.
- `budget_exhausted`: stop on the shared campaign budget.
- `no_eligible_tasks`: report remaining outcomes/blockers; do not infer success.
- `authority_required`: retain the unresolved choice or missing grant.
- `unsafe_repository`: preserve dirt and stop unsafe writes.
- `unknown_external_effect`: reconcile the effect before further external writes.

The contract contains no parallelism setting: this design permits one builder
and one owned task worktree at a time. Future runtime work must consume these
bounds; existing `auto`/`loop` commands do not yet enforce this new campaign
contract merely because its validation passes.

## Governing decision and adoption

Design disposition for autonomy-01: **ready** within the contract-only scope.
The source plan `.go/plans/autonomy-first.json`, the accepted `autonomous-campaign`
brief and `bounded-serial-campaign-contract-v1` decision, the accepted
`material-work-requires-governing-decision-v1` decision,
ABC decisions d03–d10 and `explicit-task-decisions-govern-readiness-v1` supply
the governing constraints. The observable change is that an explicit serial
mandate can now be validated against existing outcomes and authority boundaries;
previously only individual execution contracts and loose planning prose existed.
The tests cover planning/execution boundaries, unresolved versus delegated
choices, revisions and drift, missing references, model selection, delivery
obligations, malformed input and legacy no-op behavior.

The controller owns canonical decision/brief mutations. It recorded the following
decision on 2026-09-19 and adopted the brief with that exact decision reference.
The canonical `.go/decisions/events.jsonl` and
`.go/architecture/briefs/autonomous-campaign.json` provide the readback; an owned
worktree's copied `.go` can still contain the earlier draft. This adoption does
not waive dependent tasks' readiness, conformance or authority gates.

```json
{
  "decision_id": "bounded-serial-campaign-contract-v1",
  "title": "Bind serial campaigns to explicit authority and existing outcome proof",
  "status": "accepted",
  "context": "autonomy-01 defines the minimum campaign contract before dependent runtime work. Existing ABC task, workspace, completion and publication records remain authoritative. The autonomy-first plan prefers reliable serial progress and forbids unlimited work from vision.",
  "decision": "Use opt-in immutable go-workflow.campaign-contract.v1 revisions to bind original intent, adopted goal outcomes, canonical task R# text references, applicable vision/principles and exact accepted decisions. Bound execution with an explicit task allowlist, outcome-linked research/repair allowances, selected model profiles, shared budget, separate release/deployment grants and mandatory stops. Preserve unresolved material decisions and use existing delegation and architecture gates. No duplicate task queue or outcome status store.",
  "consequences": [
    "Validation is read-only and cannot grant authority, prove completion or start a worker. Existing historical contracts are unchanged.",
    "The serial controller must retain immutable revisions and cumulative consumption, validate current references, admit bounded expansion before execution and apply existing task/workspace/architecture/release gates.",
    "An empty eligible queue stops with unresolved outcomes; only content-bound behavioral, critic and required delivery evidence can establish goal completion.",
    "Parallel builders, a new approval system, implicit production destinations and GitHub Actions remain excluded."
  ]
}
```

The adopted brief retains its scope, risks and measurable quality attributes.
Its versioned contract boundary replaces the intake-only constraint, and the
resolved contract-design question is removed. The plan retains its original
planning-only source history. Live pilot bindings and any applicable named-human
architecture gate remain unresolved until their actual authority is available.
The controller records conformance through the existing architecture lane.
Contract tests support intent/authority integrity; they do not prove serial
continuation, recovery, delivery or the live campaign. Those require dependent
task proof.

R1/R2 are implemented by the schema, validator and fixtures. R3 has the adopted
governing decision/brief and positive/negative/legacy validation evidence.
R4 remains the controller's critic,
final-candidate checks, scoped commit/push, immutable release and readback. No
release version or changelog is changed by the build worker.
