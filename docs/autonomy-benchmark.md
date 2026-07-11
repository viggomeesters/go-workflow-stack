# Bare Go Autonomy Benchmark

This benchmark states what the stack proves against the desired Ralph / Oh-My-Codex style loop. It is deliberately blunt: green tests are not the same as full autonomous coding.

## Scale

| Mark | Meaning |
|---|---|
| `PASS` | Implemented and covered by local tests/checks. |
| `PARTIAL` | Contract or mechanical support exists, but the agent/runtime still carries part of the behavior. |
| `MISS` | Not implemented yet; claiming this would be bullshit. |

## Benchmark matrix

| Criterion | Status | Evidence | Residual risk |
|---|---|---|---|
| One prompt routes repo-local work from `go` | `PASS` | `cli/go.py go`, router tests for `go`/`GOO`/`go-loop` | Hermes still needs to invoke the CLI/skill correctly in every runtime. |
| `.go` is source of truth | `PASS` | `validate`, `status`, task lifecycle tests, `.go/tasks/*` state | Runtime skill drift can still confuse behavior if not reloaded. |
| Non-mutating inspection path | `PASS` | `go --json` without `--write` returns `proposed_task` and leaves git clean | Interactive UX must make write/execute intent obvious. |
| Intent can create missing task | `PASS` | `go --write --intent ...` materializes `.go/tasks/open/<id>.json` | Task quality is only as good as rough intent parsing. |
| Multi-task mechanical execution | `PASS` | `auto --execute --max-tasks 2` smoke proves two verification-ready tasks complete | Mechanical executor does not synthesize code changes. |
| Budget/checkpoint envelope | `PASS` | `run_envelope.budget`, `commands_run`, checkpoints in tests | Budgets are CLI-local; no durable resume queue yet. |
| Safety gate for dirty/secret state | `PASS` | dirty `.env` test blocks execution | Secret detection is heuristic. |
| Critic evidence on failure | `PASS` | failing verification test records `auto.attempt` with `critic`, `repair`, `judge`, then blocks | Critic is mechanical; not semantic code review. |
| Real build/edit step inside executor | `PARTIAL` | Handoff and attempt ledger model build stage | Actual editing is still performed by Hermes/Bertus as the agent, not by the Python CLI. |
| Recheck/devil semantic review | `PARTIAL` | Skill/contract requires it; attempts include critic/judge fields | No embedded LLM reviewer or external reviewer adapter yet. |
| Repair attempts like Ralph ladder | `PARTIAL` | Attempts have `strategy=direct_fix` and block with repair hints | No automatic re-approach/simplify/last-stand loop yet. |
| Commit/push per logical task | `PARTIAL` | Agent workflow used commits/pushes; docs require policy | CLI does not itself commit/push; should remain policy-gated. |
| Oh-My-Codex/Ralph equivalence | `MISS` | N/A | Stack is now loop-ready and instrumented, not a full autonomous coding runtime. |

## Verdict

Current level: **loop-ready conductor + mechanical verifier**, not a complete Ralph/Oh-My-Codex clone.

The honest claim is:

> Viggo can use `go` / `go-loop` as the control-handoff language. The stack can route, create tasks, validate state, execute verification-ready tasks, record critic attempts, and stop safely. Full semantic build/critic/repair remains the responsibility of the Hermes/Bertus agent following the repo-local skill until a richer executor adapter exists.

## Promotion criteria for “fully autonomous enough”

The stack can claim full-enough autonomy only when all are true:

1. A fresh fixture starts with a failing task.
2. One `go-loop --execute` run records build, verify, critic, repair, judge attempts.
3. At least one failure is repaired or transformed into a precise same-scope follow-up without user intervention.
4. Recheck/devil findings are machine-readable and block finish when major.
5. Final state includes task evidence, reflection, clean git or intentional commit, and compact result.

Until then, any “helemaal autonoom” claim should be downgraded to **partial**.
