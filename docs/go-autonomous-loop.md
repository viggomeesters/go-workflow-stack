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

CLI-only `auto --execute` can handle mechanical verification-ready tasks, but real coding requires an executor handoff. The critical improvement is that Hermes/Bertus must treat the handoff as an instruction to start tools now, not as a pretty JSON plan for Viggo to manually run.
