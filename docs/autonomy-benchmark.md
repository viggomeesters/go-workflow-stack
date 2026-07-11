# Bare Go Autonomy Benchmark

This benchmark states what the stack proves against the desired Ralph / Oh-My-Codex style loop. It is deliberately blunt: green tests are not the same as unconstrained autonomy; the claim must stay tied to the proven runtime boundary.

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
| Budget/checkpoint envelope | `PASS` | `run_envelope.budget`, `commands_run`, checkpoints in tests | Needs stronger in-attempt budget enforcement and durable resume proof. |
| Safety gate for dirty/secret state | `PASS` | dirty `.env` test blocks execution | Secret detection is heuristic. |
| Critic evidence on failure | `PASS` | failing verification test records `auto.attempt` with `critic`, `repair`, `judge`, then blocks | Critic is only semantic when built-in or external critic is configured. |
| Adapter-boundary build/edit executor | `PASS` | `--build-command` and `--repair-command` adapter hooks; repair fixture edits a file and recovers | The stack supplies hooks; actual intelligence comes from configured adapter command. |
| Full autonomous coding runtime | `PARTIAL` | Bounded conductor + adapter hooks + failing-task repair test | Missing default Codex/Hermes adapter, rich artifacts, semantic judge, follow-up generation, resume, and ship policy proof. |
| Recheck/devil semantic review | `PARTIAL` | `--critic-command` hook can block/repair after verification; attempt ledger records critic output | No default semantic critic yet. |
| Repair attempts like Ralph ladder | `PARTIAL` | `--max-attempts`, strategy ladder, repair fixture: fail → repair → pass | Strategies are recorded; adapter quality determines repair depth. |
| Commit/push per logical task | `PARTIAL` | Agent workflow used commits/pushes; docs require policy | CLI intentionally does not auto-push by default; needs explicit ship policy. |
| Oh-My-Codex/Ralph equivalence | `PARTIAL` | Control-loop architecture and adapter boundary exist | Not equivalent until the remaining runtime gaps below are implemented and proven. |

## Current verdict

Current level: **Ralph/Oh-My-Codex-inspired `.go` conductor with adapter hooks — not yet a full autonomous coding runtime.**

The honest claim is:

> Viggo can use `go` / `go-loop` as the control-handoff language. The stack can route, create tasks, validate state, execute multi-task loops, run build/critic/repair adapters, record attempt evidence, and stop safely. Full Ralph/Oh-My-Codex equivalence requires default semantic adapters, rich attempt artifacts, follow-up task generation, resume state, and explicit ship policy.

## Gaps that must be green before full equivalence

1. Default Codex/Hermes repair adapter command, not only raw shell hooks.
2. Built-in semantic critic/judge that can block first-green results.
3. Per-attempt artifact ledger: `prompt.md`, `verify.log`, `critic.md`, `diff.patch`, `verdict.json`.
4. Real codebase repair fixture, not a toy single text-file replacement.
5. Critic findings converted into scoped `.go/tasks/open/*.json` follow-ups.
6. Machine-readable ship policy for none/local-commit/push.
7. Durable resume state with run id, remaining budget, and exact resume command.

Until these are implemented, any blanket “Ralph/Oh-My-Codex equivalent” claim is overclaim.
