# Task execution contracts

The optional `execution_contract` adds typed execution intent to a task. Its
schema is `go-workflow.execution-contract.v1`. This release implements data
validation, intake preservation and dependency readiness. Actual per-model
dispatch, task worktrees, crash checkpoints and release publication belong to
the subsequent ABC tasks. A valid model selection is not evidence that a model
ran; a `release.mode` declaration alone does not enforce shipping in the legacy
finish engine.

## Selection and compatibility

```json
{
  "schema": "go-workflow.execution-contract.v1",
  "task_kind": "product",
  "model": {"id": "gpt-6-astra", "effort": "high"},
  "release": {"mode": "required", "profile": "project-release"},
  "workspace": {
    "mode": "task_worktree",
    "base_branch": "main",
    "control_state": "repo_local_single_writer"
  }
}
```

`main` and the model above are examples, not hidden defaults. Supported effort
names are a structural vocabulary; the later adapter capability check must
validate the actual model/effort combination before invoking a worker.

Projects can set `execution_defaults` to a complete contract. Task intake merges
defaults with explicit overrides and stores the full result. Within `model`,
`critic_model`, `release` and `workspace`, task fields override project fields;
other fields replace the corresponding value. Subsequent default changes do not
rewrite existing task selections. `critic_model` is optional.

Use `task create --execution-contract profile.json` for a JSON override file.
Execution-brief work units accept `execution_contract` and `dependencies`.
Recommendation storage and promotion retain those work-unit fields. Free-text
intent and spike tasks inherit explicit project defaults; the runtime does not
guess a model from arbitrary prose. Critic follow-ups inherit their parent's
explicit contract and dependencies. JSON export retains the task fields.

The contract is opt-in. Existing tasks without `execution_contract`, including
historical done records and the template's source smoke fixture, are not
rewritten or retroactively required to prove a release. `product` requires
`release.mode: required`. `mechanical`, `no_change` and `smoke` may explicitly
select `mode: none`, but must supply a non-empty policy reason. A no-change
product task is not silently converted into a fake release. Product version,
workflow version, task ID and run ID are separate identities.

## Dependencies

```json
[
  {
    "project": "my-project",
    "task_id": "previous-task",
    "requires": "done_with_required_release_evidence"
  }
]
```

`task create --dependencies dependencies.json` accepts this array. Explicit
dependency intake requires a task contract or project execution defaults.
Historical planning-only dependency metadata remains inert until its task is
explicitly opted in.

The validators reject duplicate references, missing tasks, identity mismatches,
missing participants and cycles before task intake writes. A batch is checked
in full, including forward references, before its first task is written. Local
references use the current project's ID. Cross-project references require an
explicit `dependency_projects` map in project.json, for example
`{"shared-library": "../shared-library"}`. Paths are resolved relative to the
declaring repository; no global vault or inferred sibling queue is consulted.
Each referenced project must in turn declare its own external participants.

`next` and automatic selection omit tasks whose dependencies are not done and,
when work/review fields exist, completed and approved. Claim rechecks this under the task lock.
No eligible tasks is reported as dependency-blocked, not completion. Existing
legacy tasks without a review field retain their historical interpretation.

`done_with_required_release_evidence` additionally requires a bound receipt:

```json
{
  "schema": "go-workflow.release-receipt.v1",
  "task_id": "previous-task",
  "project": "my-project",
  "status": "verified",
  "commit": "0123456789012345678901234567890123456789",
  "evidence": [".go/evidence/previous-task-release.json"]
}
```

Readiness checks receipt identity, shape and references; it does not contact a
remote publisher or authenticate manually authored receipts. The shipping
runtime must create receipts only after independent external readback. Do not
fabricate them to satisfy a dependency. A missing receipt is a blocker.

## Phases and proof

Project `phase_profiles` maps profile names to ordered phase definitions.
An execution contract can reference one through `phase_profile`. Every phase
declares `inputs`, `outputs`, `required_evidence`, `stop_conditions`, `handoff`,
`scope.read`, `scope.modify` and `required_outcomes`, plus schema and ID.
See `schemas/phase-contract.schema.json`. These are data interfaces for later
phase execution, not a requirement to invoke every historical skill.

`verification_evidence` on a task is an array described by
`schemas/verification-evidence.schema.json`. A record identifies the task,
phase and covered requirements. `missing`, `failed`, `not_applicable` and
`passed` are different states. Non-applicability needs a concrete policy
reason. Executed checks identify command, cwd, revision, worktree digest,
exit code and evidence references; `passed` requires exit code zero. A record
for another task is invalid.

Schema/skill lint validates document structure. Test evidence documents command
execution. Release proof establishes the final published revision. They must
not substitute for one another. Evidence invalidation after edits/rebase and
mandatory phase/requirement enforcement at finish are subsequent lifecycle
work, not claims made by this contract-only increment.

## Verification

Run `python3 -m pytest tests/test_abc_contracts.py
tests/test_abc_phase_contracts.py -q` and the repository's normal checks.
The tests use temporary repositories and synthetic receipts, without model
calls or external publication. Existing release checks still require Linux
`memfd` support. A successful macOS contract test run does not replace that
release gate.

Legacy provenance and exclusions are recorded in
`.go/plans/legacy-insights.json`. Selected ideas are translated into the current
JSON runtime; historical vault writers and monolithic pipelines are not loaded.
