# Bare Go Autonomy Benchmark

This benchmark states what the stack proves against the desired Ralph / Oh-My-Codex style loop. It is deliberately blunt: green tests are not the same as unconstrained autonomy; the claim must stay tied to the proven adapter boundary.

## Scale

| Mark | Meaning |
|---|---|
| `PASS` | Implemented and covered by local tests/checks. |
| `PARTIAL` | Contract or support exists, but some behavior still depends on an external agent/runtime or policy. |
| `MISS` | Not implemented yet; claiming this would be bullshit. |

## Benchmark matrix

| Criterion | Status | Evidence | Residual risk |
|---|---|---|---|
| One prompt routes repo-local work from `go` | `PASS` | `cli/go.py go`, router tests for `go`/`GOO`/`go-loop` | Hermes still needs to invoke the CLI/skill correctly in every runtime. |
| `.go` is source of truth | `PASS` | `validate`, `status`, task lifecycle tests, `.go/tasks/*` state | Runtime skill drift can still confuse behavior if not reloaded. |
| Non-mutating inspection path | `PASS` | `go --json` without `--write` returns `proposed_task` and leaves git clean | Interactive UX must make write/execute intent obvious. |
| Intent can create missing task | `PASS` | `go --write --intent ...` materializes `.go/tasks/open/<id>.json` | Task quality is only as good as rough intent parsing. |
| Multi-task mechanical execution | `PASS` | `auto --execute --max-tasks 2` smoke proves two verification-ready tasks complete | Mechanical path still depends on task verification quality. |
| Budget/checkpoint envelope | `PASS` | `run_envelope.budget`, `commands_run`, checkpoints in tests | Budgets are CLI-local; durable daemon resume is out of scope. |
| Safety gate for dirty/secret state | `PASS` | dirty `.env` test blocks execution | Secret detection is heuristic. |
| Critic evidence on failure | `PASS` | failing verification test records `auto.attempt` with `critic`, `repair`, `judge`, then blocks | Critic is only semantic when a critic adapter is configured. |
| Real build/edit step inside executor | `PASS` | `--build-command` and `--repair-command` adapter hooks; repair fixture edits `answer.txt` and recovers | The stack supplies hooks; actual intelligence comes from the configured adapter command. |
| Recheck/devil semantic review | `PASS` | `--critic-command` adapter hook can block/repair after verification; attempt ledger records critic output | No built-in LLM; semantic quality depends on adapter. |
| Repair attempts like Ralph ladder | `PASS` | `--max-attempts`, strategy ladder, repair fixture: fail → repair → pass without user intervention | Strategies are recorded; adapter quality determines repair depth. |
| Commit/push per logical task | `PARTIAL` | Agent workflow used commits/pushes; docs require policy | CLI intentionally does not auto-push by default because public/destructive boundaries need policy. |
| Oh-My-Codex/Ralph equivalence | `PASS` | Bounded conductor + adapter hooks + failing-task repair test | Equivalent in architecture/control-loop shape, not identical to any external product implementation. |

## Verdict

Current level: **Ralph/Oh-My-Codex-style autonomous coding runtime via adapter boundary**.

The honest claim is:

> Viggo can use `go` / `go-loop` as the control-handoff language. The stack can route, create tasks, validate state, execute multi-task loops, run build/critic/repair adapters, recover a failing task without user intervention, record attempt evidence, and stop safely. Full intelligence is supplied by the configured adapter command or by Hermes/Bertus following the repo-local skill; the conductor now has the runtime slots and proof harness for that intelligence.

## Proven promotion criteria

The previous promotion criteria are now covered:

1. A fresh fixture starts with a failing task. ✅
2. One `go-loop --execute` run records build, verify, critic, repair, judge attempts. ✅
3. At least one failure is repaired without user intervention. ✅
4. Recheck/devil findings are machine-readable via critic adapter and block finish when major. ✅
5. Final state includes task evidence, reflection, and compact result. ✅

## Remaining honest limits

- No built-in LLM is embedded in the Python CLI. Use `--build-command`, `--critic-command`, and `--repair-command` to connect Codex, Hermes, Claude, local scripts, or another executor.
- Auto-commit/push remains policy-gated. This is intentional; public/destructive side effects should not be hidden behind a generic loop.
- Adapter commands must be deterministic enough to verify. If an adapter cannot produce a passing check, the loop blocks with evidence instead of greenwashing.
