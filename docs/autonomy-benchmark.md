# Bare Go Autonomy Benchmark

This benchmark states what the stack proves against the desired Ralph / Oh-My-Codex style loop. It is deliberately blunt: green tests are not the same as unconstrained autonomy; claims must stay tied to the proven runtime boundary.

## Scale

| Mark | Meaning |
|---|---|
| `PASS` | Implemented and covered by local tests/checks. |
| `PARTIAL` | Contract or support exists, but some behavior still depends on external model quality, local adapter availability, or explicit policy. |
| `MISS` | Not implemented yet; claiming this would be bullshit. |

## Benchmark matrix

| Criterion | Status | Evidence | Residual risk |
|---|---|---|---|
| One prompt routes repo-local work from `go` | `PASS` | `cli/go.py go`, router tests for `go`/`GOO`/`go-loop` | Hermes still needs to invoke the CLI/skill correctly in every runtime. |
| `.go` is source of truth | `PASS` | `validate`, `status`, task lifecycle tests, `.go/tasks/*` state | Runtime skill drift can still confuse behavior if not reloaded. |
| Non-mutating inspection path | `PASS` | `go --json` without `--write` returns `proposed_task` and leaves git clean | Interactive UX must make write/execute intent obvious. |
| Intent can create missing task | `PASS` | `go --write --intent ...` materializes `.go/tasks/open/<id>.json` | Task quality is only as good as rough intent parsing. |
| Multi-task mechanical execution | `PASS` | `auto --execute --max-tasks 2` smoke proves two verification-ready tasks complete | Mechanical path still depends on task verification quality. |
| Hard command and time budget | `PASS` | Command-budget and process-group timeout tests prove a bounded run stops before extra work and kills hung verification | External tools can still spend their allowance inefficiently. |
| Safety gate for dirty/secret state | `PASS` | dirty `.env` test blocks execution | Secret detection is heuristic. |
| Adapter availability proof | `PASS` | `agent-check --json` reports real Codex/Hermes availability; missing Codex blocks instead of fake-green | Adapter quality/config remains local-machine dependent. |
| Adapter-boundary build/edit executor | `PASS` | `--build-command`, `--repair-command`, and availability-gated `--repair-agent codex/hermes` | External tool quality determines code quality. |
| Dangerous adapter bypass avoided | `PASS` | Codex template no longer uses `--dangerously-bypass-approvals-and-sandbox`; test asserts absence | Codex CLI flags can change upstream. |
| Diff/scope enforcement after adapters | `PASS` | Tests cover read-only paths, glob scopes, newly dirty paths, and modification of pre-existing unrelated dirt | Generated-file ignore list may need expansion. |
| Semantic critic/judge | `PASS` | Built-in structural critic is enabled by default; agent-mode tasks also run the selected Codex/Hermes adapter as a separate deep critic that must return PASS or BLOCK | Semantic quality still depends on the selected model and prompt. |
| Follow-up task generation | `PASS` | `--followup-on-block` creates scoped `.go/tasks/open/*.json` from critic findings | Follow-up granularity is heuristic. |
| Per-attempt artifact ledger | `PASS` | `.go/runs/<task-id>/attempt-XX/{prompt.md,verify.log,critic.md,diff.patch,verdict.json}` | Large diffs/logs may need pruning later. |
| Real codebase repair fixture | `PASS` | Mini Python package failing pytest is repaired by go-loop without user intervention | Fixture is small; larger repos still depend on adapter. |
| Repair attempts like Ralph ladder | `PASS` | `--max-attempts`, strategy ladder, repair fixtures: fail → repair → pass | Strategy names are recorded; adapter decides actual technique. |
| Exact resume state | `PASS` | `.go/runs/latest.json` stores effective flags and resume command including budgets, repair flags, critic/follow-up, ship policy, allow flags | Resume does not restore external process env beyond command/flags. |
| Restartable multi-task campaign | `PASS` | A two-stage release-notes fixture builds, receives a blocking critic verdict, repairs, verifies, commits, stops on task budget, executes the persisted resume command, completes the second task, and passes the goal audit | The adapter is deterministic so orchestration failures are reproducible; live-model quality remains separate. |
| Linux/Hermes contract | `PASS` | `bash scripts/check-linux.sh` runs the full suite and stack/template pairing locally with Python 3.11+; `go doctor` verifies host readiness | Live Hermes model quality remains an explicit opt-in local check. |
| Scoped transactional ship policy | `PASS` | Tests prove unrelated dirt is not committed, unauthorized push leaves the verified task active, and failed local commits restore task/evidence state | A push can still fail after a valid local commit and require an explicit retry. |
| Vision/principles execution context | `PASS` | Every build/critic/repair hook receives `GO_CONTEXT_JSON`; attempt `prompt.md` also records north star, metrics, principles, hierarchy, and task | Adapters must actually obey the supplied contract. |
| Template-to-project pairing | `PASS` | Pairing check executes first `auto`; `spike` and `apply-template.sh` replace template identity with project-specific vision/tasks | User intent still determines whether the generated vision is useful. |
| Vision-level completion audit | `PASS` | Final audit requires no open/active/blocked tasks, evidence on every done task, valid cross-file contract, declared success metrics, and passing project-level verification | Textual success metrics are structurally present, not semantically proven without a deep critic adapter. |
| Oh-My-Codex/Ralph-style integrated runtime | `PARTIAL` | Hardened conductor, safe default agent selection, deep critic, adapter context, budgets, scope, executable resume, restartable multi-task campaign, transactional shipping, and goal audit are covered locally | A real non-fake adapter campaign and broader repo diversity are not yet benchmarked. |
| Unconstrained self-improving agent | `PARTIAL` | Can plug Codex/Hermes; `.go` controls state and evidence | The Python CLI does not embed an LLM or bypass safety gates. |

## Current verdict

Current level: **hardened `.go` conductor with a real adapter boundary; integrated Ralph/Oh-My-Codex-level coding autonomy remains partial.**

The honest claim is:

> Viggo can use `go` / `go-loop` as the control-handoff language. The stack now reliably conducts bounded and restartable task execution, selects a safe default coding adapter, deep-criticises first green, commits transactionally, and passes durable project context into adapters. It is Ralph/OMX-like in control-loop shape, but should not claim universal equivalence until live-model campaigns across varied repositories are benchmarked.

## Green criteria now covered

1. Default Codex/Hermes repair adapter command with real availability reporting. ✅
2. Built-in semantic critic/judge that can block first-green results. ✅
3. Per-attempt artifact ledger: `prompt.md`, `verify.log`, `critic.md`, `diff.patch`, `verdict.json`. ✅
4. Real codebase repair fixture, not a toy single text-file replacement. ✅
5. Critic findings converted into scoped `.go/tasks/open/*.json` follow-ups. ✅
6. Machine-readable ship policy for none/local-commit/push with scoped staging. ✅
7. Durable resume state with exact effective flags and resume command. ✅
8. Adapter diff/scope enforcement after build/critic/repair hooks. ✅
9. Hard command-budget and process-group timeout enforcement. ✅
10. No dangerous Codex bypass flag in the default adapter template. ✅
11. Cross-file project/hierarchy/task coherence validation. ✅
12. Transactional task/ship behavior for policy and local-commit failures. ✅
13. Vision, architecture principles, hierarchy, evidence, decisions, and task context passed to every adapter. ✅
14. Vision-level structural completion audit plus project-wide verification before `done`. ✅

## Honest limits

- No model is embedded in the Python CLI. Agent-mode tasks select an installed Codex or Hermes CLI by default; explicit build, critic, and repair commands remain available for deterministic or custom adapters.
- `push` remains behind `--allow-push`; this is a safety feature, not a missing autonomous capability.
- The built-in semantic critic is intentionally conservative. Agent-mode tasks add a separate read-only Codex/Hermes review; `--critic-command` remains available for a custom judge.
- Real-world large-repo performance still depends on adapter quality, test quality, and task scope quality.
- Adapter selection and the deep-critic protocol are covered with deterministic CLI fixtures; a campaign against a real model remains deliberately unclaimed.
- Task exhaustion is not yet a semantic audit that the vision itself has been achieved.
