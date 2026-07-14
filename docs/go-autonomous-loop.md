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

Tasks declare `execution_mode: mechanical|agent`. Mechanical tasks execute only their explicit commands. Agent tasks without a build command select an installed Codex adapter first and Hermes second (`--executor-agent` can override or disable this). Codex build/repair runs are ephemeral with `workspace-write`; the subsequent deep critic is a distinct ephemeral `read-only` run and must emit an explicit PASS or BLOCK verdict. A blocking verdict re-enters the repair loop instead of accepting first green.

The built-in semantic critic is enabled by default. A structurally valid task with generic acceptance is stopped by a pre-claim `contract_gate`; it is not moved to active or blocked and no verification command runs. If a task budget expires while open work remains, the result is `budget_exhausted` with a persisted resume command, never `done`.

After the final task, the conductor runs a goal-completion audit. `done` requires: no open, active, or blocked tasks; valid project/vision/principle/hierarchy/task links; evidence on every done task; declared vision success metrics; and passing project-level default verification. A failing final check returns `goal_incomplete` and requires concrete follow-up work. This is a structural and executable audit; semantic proof of free-text success metrics still belongs to a deep critic adapter.

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
