# Bare Go Autonomy Benchmark

This benchmark states what the stack proves against the desired Ralph / Oh-My-Codex style loop. It is deliberately blunt: green tests are not the same as unconstrained autonomy; the claim must stay tied to the proven runtime boundary.

## Scale

| Mark | Meaning |
|---|---|
| `PASS` | Implemented and covered by local tests/checks. |
| `PARTIAL` | Contract or support exists, but some behavior still depends on external model quality or policy. |
| `MISS` | Not implemented yet; claiming this would be bullshit. |

## Benchmark matrix

| Criterion | Status | Evidence | Residual risk |
|---|---|---|---|
| One prompt routes repo-local work from `go` | `PASS` | `cli/go.py go`, router tests for `go`/`GOO`/`go-loop` | Hermes still needs to invoke the CLI/skill correctly in every runtime. |
| `.go` is source of truth | `PASS` | `validate`, `status`, task lifecycle tests, `.go/tasks/*` state | Runtime skill drift can still confuse behavior if not reloaded. |
| Non-mutating inspection path | `PASS` | `go --json` without `--write` returns `proposed_task` and leaves git clean | Interactive UX must make write/execute intent obvious. |
| Intent can create missing task | `PASS` | `go --write --intent ...` materializes `.go/tasks/open/<id>.json` | Task quality is only as good as rough intent parsing. |
| Multi-task mechanical execution | `PASS` | `auto --execute --max-tasks 2` smoke proves two verification-ready tasks complete | Mechanical path still depends on task verification quality. |
| Budget/checkpoint envelope | `PASS` | `run_envelope.budget`, `commands_run`, checkpoints in tests | Runtime quality still depends on realistic budgets. |
| Safety gate for dirty/secret state | `PASS` | dirty `.env` test blocks execution | Secret detection is heuristic. |
| Critic evidence on failure | `PASS` | failing verification test records `auto.attempt` with `critic`, `repair`, `judge`, then blocks | Semantic depth depends on critic adapter/model. |
| Adapter-boundary build/edit executor | `PASS` | `--build-command`, `--repair-command`, and `--repair-agent codex/hermes` | External tool quality determines code quality. |
| Default repair agent route | `PASS` | `--repair-agent codex` / `--repair-agent hermes` options build scoped repair prompts | Requires those CLIs/config to exist in the runtime environment. |
| Semantic critic/judge | `PASS` | `--semantic-critic` blocks generic/default acceptance and missing verification; `--critic-command` supports external judges | Built-in critic is conservative; deep semantic review should use adapter. |
| Follow-up task generation | `PASS` | `--followup-on-block` creates scoped `.go/tasks/open/*.json` from critic findings | Follow-up granularity is heuristic. |
| Per-attempt artifact ledger | `PASS` | `.go/runs/<task-id>/attempt-XX/{prompt.md,verify.log,critic.md,diff.patch,verdict.json}` | Large diffs/logs may need pruning later. |
| Real codebase repair fixture | `PASS` | Mini Python package failing pytest is repaired by go-loop without user intervention | Fixture is small; larger repos still depend on adapter. |
| Repair attempts like Ralph ladder | `PASS` | `--max-attempts`, strategy ladder, repair fixtures: fail → repair → pass | Strategy names are recorded; adapter decides actual technique. |
| Resume state | `PASS` | `.go/runs/latest.json` stores status, completed tasks, budget, and resume command | Durable daemon/queue is out of scope. |
| Commit/push per logical task | `PASS` | `--ship-policy none/local-commit/push`; push requires `--allow-push`; local commit test proves clean git | Public push remains intentionally explicit. |
| Oh-My-Codex/Ralph-style runtime | `PASS` | Control-loop conductor, adapter hooks, semantic critic, follow-ups, artifact ledger, real repair fixture, resume state, ship policy | Equivalent in local workflow shape, not a clone of external products. |
| Unconstrained self-improving agent | `PARTIAL` | Can plug Codex/Hermes; `.go` controls state and evidence | The Python CLI does not embed an LLM or bypass safety gates. |

## Current verdict

Current level: **Ralph/Oh-My-Codex-style `.go` autonomous coding runtime with explicit adapter boundary.**

The honest claim is:

> Viggo can use `go` / `go-loop` as the control-handoff language. The stack can route, create tasks, validate state, execute multi-task loops, run build/critic/repair adapters, use Codex/Hermes repair-agent command templates, record rich attempt artifacts, generate follow-up tasks from critic findings, persist resume state, and ship according to explicit policy. It is Ralph/OMX-like in workflow shape; model intelligence remains an adapter, not hidden magic inside the Python CLI.

## Green criteria now covered

1. Default Codex/Hermes repair adapter command, not only raw shell hooks. ✅
2. Built-in semantic critic/judge that can block first-green results. ✅
3. Per-attempt artifact ledger: `prompt.md`, `verify.log`, `critic.md`, `diff.patch`, `verdict.json`. ✅
4. Real codebase repair fixture, not a toy single text-file replacement. ✅
5. Critic findings converted into scoped `.go/tasks/open/*.json` follow-ups. ✅
6. Machine-readable ship policy for none/local-commit/push. ✅
7. Durable resume state with run id/status and exact resume command. ✅

## Honest limits

- No built-in LLM is embedded in the Python CLI. Use `--repair-agent codex`, `--repair-agent hermes`, `--build-command`, `--critic-command`, or `--repair-command` to connect the actual intelligence.
- `push` remains behind `--allow-push`; this is a safety feature, not a missing autonomous capability.
- The built-in semantic critic is intentionally conservative. For deep code review, use `--critic-command` with a real reviewer/LLM adapter.
