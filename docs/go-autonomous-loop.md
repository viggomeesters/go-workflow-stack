# Go Autonomous Loop

## Target UX

Viggo should be able to say only:

```text
go
```

The agent then resolves whether this is a loose/small command or repo-local project work. If the target repo has `.go/project.json`, `.go` wins and the agent enters an autonomous loop instead of asking Viggo to drip-feed `next`, `claim`, `recheck`, `devil`, `finish`, or `selfimprove` commands.

## Non-negotiable contract

`go` is not a feature generator. It is a bounded engineering control handoff.

The agent must first establish or repair the project contract:

1. `.go/vision.json` exists and states the end goal / north star.
2. `.go/architecture-principles.json` exists and constrains design choices.
3. `.go/hierarchy.json` links epics/features/tasks.
4. A concrete `.go/tasks/open/*.json` task exists for the current executable slice.
5. The end-result goal and acceptance evidence are explicit before execution.

Only after that does execution start.

## Router semantics

```text
go
├─ no obvious repo / simple loose request
│  └─ handle directly or ask one blocker question only when needed
├─ repo exists, no .go
│  └─ spike/adopt minimal .go vision + principles + first task
├─ repo has .go but contract incomplete
│  └─ repair vision/principles/hierarchy/task first
└─ repo has valid .go and executable work
   └─ enter autonomous loop
```

## Autonomous loop

For every task slice:

```text
SELECT next executable task
CLAIM task
READ task scope + acceptance + verification
IMPLEMENT only that scope
VERIFY focused checks + repo guardrails
RECHECK delivered result against acceptance
DEVIL/critic: find why first green is insufficient
REPAIR if blocker/major finding is in scope
VERIFY again
COMMIT/PUSH according to repo policy
FINISH task with evidence
REFLECT: should vision/principles/hierarchy/tasks/skills improve?
CONTINUE to next task unless done, blocked, or budget exhausted
```

## Capacity policy

The autonomous loop defaults to **solo** or **lead-plus-one-worker** execution. More agents are not automatically better: parallel builders are allowed only when normalized selected-task `scope.modify` paths are disjoint. Requested builder count never overrides detected overlap.

The emitted `auto` / `go-loop` handoff exposes `execution_policy.capacity` so a Hermes, Codex, Claude, or Gemini conductor can see whether it should run solo, keep one reviewer lane open, or fan out to disjoint builders. Broad or overlapping scopes collapse back to serial execution with a reviewer lane.

See [`agent-team-capacity-patterns.md`](agent-team-capacity-patterns.md) for the mined public-safe pattern and rollout order.

This is the old go-workflow phase discipline in a lighter repo-local form:

```text
SETUP → PLAN → ROUTE/CLAIM → BUILD → VERIFY → DOCS/LEDGER → DEVIL → ANTISLOP → SHIP → BETTER
```

The new stack must not revive the token-burning ceremony of phase skills as mandatory prompts. It should preserve the consistency: every phase has a gate, evidence, and a stop condition.

## Ralph / Oh-My-Codex inspiration

The useful part is persistence, not noise.

Default retry ladder:

1. `direct_fix` — repair the concrete failing gate.
2. `re_approach` — change implementation strategy while keeping scope.
3. `simplify` — remove accidental complexity and satisfy the canonical contract.
4. `last_stand` — targeted rewrite of the load-bearing part.
5. `block_with_evidence` — stop honestly with exact failing gate/input.

A pass requires:

- acceptance met;
- verification commands pass;
- critic/devil has no blocking findings;
- git state is clean/aligned or intentionally committed;
- evidence is recorded in `.go/evidence/events.jsonl` / `.go/runs/events.jsonl`.

Every build, critic, and repair adapter receives a single `GO_CONTEXT_JSON` contract containing the project, vision, architecture principles, hierarchy, selected task, recent evidence, and recent decisions. The same north star, success metrics, principles, and hierarchy are recorded in each attempt's `prompt.md`. The task is not an isolated prompt fragment: durable project direction must be visible at execution time and auditable afterwards.

Tasks declare `execution_mode: mechanical|agent`. Mechanical tasks execute only their explicit commands. Agent tasks without a build command select an installed Codex adapter first and Hermes second (`--executor-agent` can override or disable this). Codex build/repair runs are ephemeral with `workspace-write`; the subsequent deep critic is a distinct ephemeral `read-only` run. Every native build, critic, and repair phase must emit a validated `go-workflow.agent-adapter-result.v1` object; a critic returns `success` or `blocked`. A blocking result re-enters the repair loop instead of accepting first green.

The built-in semantic critic is enabled by default. A structurally valid task with generic acceptance is stopped by a pre-claim `contract_gate`; it is not moved to active or blocked and no verification command runs. If a task budget expires while open work remains, the result is `budget_exhausted` with a persisted resume command, never `done`.

After the final task, the conductor runs a goal-completion audit. `done` requires: no open, active, or blocked tasks; no completed work still waiting for review disposition; valid project/vision/principle/hierarchy/task links; evidence on every done task; declared vision success metrics; and passing project-level default verification. Manual finish therefore remains review-pending until an explicit review command records `approved` or `needs_fix`. Autonomous `go-loop` records approval only after its separate critic phase passes without blocking findings. A failing final check returns `goal_incomplete` and requires concrete follow-up work. This is a structural and executable audit; semantic proof of free-text success metrics still belongs to a deep critic adapter.

At each stop, `.go/runs/latest.json` stores effective budgets, adapter selection, critic settings, ship policy, and structured resume arguments. Its command invokes `.go/runs/resume.sh`, which resolves the current machine's stack through `GO_STACK`, a sibling checkout, `~/github/go-workflow-stack`, or `~/Dev/go-workflow-stack`. A deterministic two-task campaign moves the project to a new path, selects a relocated runtime, executes the resume command, and proves the remaining task and final goal audit complete.

## What not to build

Do not turn `go` into an agent that invents random features because it has autonomy.

Autonomy is allowed for:

- choosing the next task;
- creating a missing task from clear intent;
- splitting a goal into executable slices;
- repairing verification/recheck/devil failures;
- updating docs/ledger/skills when the task changed workflow reality;
- continuing to the next safe task.

Autonomy is not allowed for:

- broad product scope expansion;
- public/destructive/payment/impersonation actions;
- production DB writes;
- credential hunting;
- building unrelated “cool” features;
- silently overriding dirty user work.

## Telegram behavior

Telegram should not become a command shell transcript.

Default output cadence:

- start: only if a real blocker or target ambiguity exists;
- during loop: silent unless checkpoint budget is reached or user input is needed;
- end: compact done/blocker report with commits, verification, and open state.

## Implementation implications

The stack needs three layers:

1. **Router** — understands `go` and chooses loose/direct vs repo-local `.go` vs spike/adopt/repair.
2. **Conductor** — owns the loop state, budgets, phase gates, task split/claim/finish, and continuation.
3. **Executor adapter** — for Hermes/Bertus/Codex/etc.; performs actual editing/review/tool calls and reports machine-readable results back to the conductor.

`auto --execute` handles both mechanical tasks and agent tasks through the safe default Codex/Hermes adapter. An emitted handoff still tells an outer Hermes/Bertus/Codex runtime to start tools now, not to return a pretty JSON plan for Viggo to run manually.

## Benchmark status

See [`autonomy-benchmark.md`](autonomy-benchmark.md) for the current Ralph / Oh-My-Codex comparison. The stack now proves a hardened conductor, default agent/critic boundary, restartable multi-task campaign, transactional commits, and a vision-level completion audit. Integrated Ralph/OMC equivalence remains `PARTIAL` because deterministic adapter fixtures do not prove the quality of every live model-driven campaign.

## Managed task phase resume (v0.3.19)

Tasks opting into `execution_contract.workspace.mode=task_worktree` use the
native Codex phase runner. Initial dispatch requires the explicit task, separate
workspace path/branch, exact base branch/commit and run identity:

```bash
./go auto . --execute --task-id T038 --agent owner --executor-agent codex \
  --workspace-path /explicit/task-workspace --workspace-branch task/T038 \
  --base-branch main --base-commit <exact-40-character-commit> --run-id T038-run \
  --max-commands 1 --max-minutes 5 --json
```

Resolve and preflight the project's immutable runtime first. The source stack
uses its documented `python3 cli/go.py` development entrypoint after preflight.
The next invocation keeps `--task-id`, `--agent` and the desired budget; omit the
initial workspace flags to reuse the stored binding. An active managed run is
selected before new open work. Ambiguous active runs require explicit selection.
An older active task without a checkpoint is never assigned a guessed phase.

The flow is setup → build → verify → critic, with bounded repair → verify loops.
Budget exhaustion after a confirmed build resumes verification, not another
builder. Native phases retain frozen model/effort profiles and durable context
references; effective model identity remains independently unconfirmed.
`resume.json` records exact task/owner arguments and the required immutable
runtime ref without mutable checkout fallbacks. The renewed budget is appended
to the run history; old usage and phase evidence remain available.

Successful build/check/critic execution currently returns `release_pending` and
keeps the task active. Verified finish is available through the controller completion commands below;
the idempotent publisher remains subsequent lifecycle work. No release, deployment or task completion is inferred from green
worker prose. Cleanup recovery only runs after verified integration, release and
approved completion; it never starts a new builder or publisher.

For an explicitly moved, stopped local pair, invoke the verified runtime with
`managed relocate <new-control> --task-id T038 --owner owner --run-id T038-run
--old-control <old-control> --old-workspace <old-worker> --workspace <new-worker>`.
The operation retains all code and historical context, repairs Git metadata,
and invalidates location-dependent checks. See `state-safety.md` for boundaries.

## Verified lifecycle completion (v0.3.20)

For tasks with an explicit execution contract, all finish and approval paths
require the same evidence. Green phase output alone leaves work active. Configure
`project.release_profiles.<name>` with provider `git-tag` or `github-release`, an
explicit remote and branch, plus `repository: owner/repo` for GitHub. Set the task's
`execution_contract.release.profile` to that name. An unknown profile blocks
required shipping; no default publisher or deployment target is inferred.

After integrating release preparation, the controller captures the actual commands:

```bash
python3 cli/go.py completion verify . --task-id T038 --agent owner --json
python3 cli/go.py completion critic . --task-id T038 --agent owner --review-file review.json
# Publish through the separately authorized release procedure, then read it back.
python3 cli/go.py completion readback . --task-id T038 --agent owner --tag v1.2.0
python3 cli/go.py completion status . --task-id T038 --agent owner
```

Before integration, pass `--workspace /registered/worker` from the control checkout;
the primary cannot substitute its own content for a ready owned worker. Verification
executes every task command with its timeout, cwd, revision, content digest, exit
code and raw output. The process guard registers each child before it starts;
a live orphan blocks new writers and finish after controller death. Commands should
be read-only checks, because the collector holds the task lock during execution.

`review.json` uses schema `go-workflow.critic-review.v1`, the exact `task_id`,
`project`, `contract_digest`, `revision` and `content_digest` returned as the verify
binding, and `status: passed`. It records a nonempty `reviewer`, `summary`,
`review_mode: same_agent` or `independent`, `blocking_findings: []`, and
`reviewed_paths` covering every changed product path. This is an explicit review
statement; hashes provide consistency, not independent reviewer or model identity
attestation. A same-agent review must say so.

Verification, critic and release references are stored in canonical task
`completion_evidence`; each requirement needs a verified outcome with an exact
validated phase reference in its evidence list. Source, version, architecture or
contract changes invalidate affected proof. Operational task, run, evidence and
planning updates do not change the product digest. Required shipping verifies the
annotated remote tag, released content, branch ancestry and, for GitHub, a published
non-draft release. Readback never publishes or rewrites refs.

Status distinguishes recorded and verified done records and excludes invalid
opted-in completion from goal success. Historical records without adoption remain
`historical_unmigrated`; status uses saved historical readback, while finish and
approval read the remote again. Ordinary clones read historical proof without
reusing old controller or process identities; the referenced Git objects and
evidence must be available. Explicit `release.mode: none` still requires its
contract reason, actual verification and review. Older runtimes do not enforce
these proofs; preserve them and use this version or newer for opted-in tasks.
