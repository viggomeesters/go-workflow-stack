---
name: repo-local-agent-workflow
description: "Use when shaping, planning, or implementing self-contained repo-local agent workflow systems: go-workflow-stack tooling, go-project-template adoption, .go contracts, JSON/JSONL project state, repo-local task lifecycle, scoped dirty/lock policy, or migration away from central vault/AW Lite execution state."
license: MIT
metadata:
  hermes:
    tags: [agent-workflow, repo-local, jsonl, planning, cli, schemas, dirty-state]
    related_skills: [go-vision, go-plan, agent-workflow-lite, grill-me]
---

# Repo-local Agent Workflow

## Overview

Use this skill when Viggo is designing or implementing a self-contained agent workflow contract inside each project repository. Repository execution requires repo-local `.go` state and fails closed when that contract is absent.

The current source split is concrete:

```text
go-workflow-stack      = reusable tooling, schemas, validators, fixtures, and this skill
go-project-template   = copyable starter `.go/` project-state repository
real project repo      = owns its own `.go/` vision/principles/hierarchy/tasks/evidence
vault / Life OS        = memory, reflection, routing, optional index; not execution SSOT
```

## Source of truth

- Stack repo: `~/github/go-workflow-stack` / https://github.com/viggomeesters/go-workflow-stack
- Template repo: `~/github/go-project-template` / https://github.com/viggomeesters/go-project-template
- Hermes runtime skill install: symlink to `~/github/go-workflow-stack/skills/repo-local-agent-workflow/SKILL.md`.

When updating the repo-local workflow rules, update this skill in `go-workflow-stack`, then verify Hermes sees the same content via `skill_view('repo-local-agent-workflow')` or `/reload-skills` in a fresh session.

## Core model

Separate these layers explicitly:

| Layer | Lives where | Purpose |
|---|---|---|
| Workflow stack/tooling | `go-workflow-stack` | schemas, CLI, validators, fixtures, repo-local workflow skill |
| Project template | `go-project-template` | copyable minimal `.go/` starter state |
| Project workflow state | target project repo, e.g. `.go/` | vision, architecture principles, hierarchy, tasks, runs, evidence, decisions |
| Vault / Life OS | vault | memory, reflection, index, cross-project context |
| Hermes/Bertus skills | Hermes runtime symlinks | operating procedure and routing |

The stack can be centralized; project execution state should be clone-local.

## Default repo-local contract

Prefer small canonical files over one mega-state file:

```text
.go/
  project.json
  architecture-principles.json
  vision.json
  hierarchy.json          # epics/features/task links; "feature_groups" accepted as legacy alias
  tasks/
    open/*.json
    active/*.json
    blocked/*.json
    done/*.json
  runs/*.jsonl
  evidence/*.jsonl
  decisions/*.jsonl       # ADR-lite decision events
  reflections/*.jsonl     # auto/loop batch self-reflection events
  imports/*.json
  locks/
```

JSON is canonical for current state. JSONL is preferred for append-only lifecycle/evidence/decision events. Markdown may be generated for humans, but it must not become the source of truth.

## Standard primitives

Use these names consistently, but keep them lightweight:

| Primitive | Canonical location | Meaning |
|---|---|---|
| Vision | `.go/vision.json` | north star, wedge, target user, promise, non-goals, success metrics |
| Principle | `.go/architecture-principles.json` | durable project constraints and enforcement rules |
| Epic | `.go/hierarchy.json` `epics[]` | large work package / feature group; not Jira ceremony |
| Task | `.go/tasks/<state>/*.json` | executable scoped unit with acceptance and verification |
| ADR-lite / Decision | `.go/decisions/events.jsonl` | append-only decision event with context, decision, consequences |
| Evidence | `.go/evidence/events.jsonl` | proof that work actually ran or shipped |
| Reflection | `.go/reflections/events.jsonl` | append-only auto/loop batch self-review and next-action trail |

ADR and Epic are standard contract concepts; do not introduce them ad hoc outside these files/events.

## Starting or retrofitting a project

Default route when a repo needs repo-local workflow state:

1. Use `go-project-template` as the starter shape.
2. Copy/adapt its `.go/` folder into the target project repo.
3. Customize `.go/project.json`, `.go/architecture-principles.json`, `.go/vision.json`, `.go/hierarchy.json`, and `.go/tasks/open/*.json`.
4. Validate with the stack:

```bash
python3 ~/github/go-workflow-stack/cli/go.py validate <target-repo>
python3 ~/github/go-workflow-stack/cli/go.py readback <target-repo>
python3 ~/github/go-workflow-stack/cli/go.py next <target-repo>
```

For the public starter template, also prove the stack/template pairing and fresh-clone check path:

```bash
python3 ~/github/go-workflow-stack/cli/go.py template-check ~/github/go-project-template --json
bash ~/github/go-project-template/scripts/check.sh
```

`go-project-template/scripts/check.sh` bootstraps a sibling `go-workflow-stack` checkout when missing; set `GO_STACK=/path/to/go-workflow-stack` to use or populate a specific stack location.

For v0.3+ end-to-end command routing, use these higher-level primitives:

```bash
python3 ~/github/go-workflow-stack/cli/go.py router <target-repo> --command GOO --intent "<rough Viggo input>" --json
python3 ~/github/go-workflow-stack/cli/go.py spike <target-repo> --brief "<rough intent>" --task-scope code
python3 ~/github/go-workflow-stack/cli/go.py auto <target-repo> --max-tasks 3 --json
python3 ~/github/go-workflow-stack/cli/go.py auto <target-repo> --max-tasks 3 --emit-handoff --json
python3 ~/github/go-workflow-stack/cli/go.py auto <target-repo> --max-tasks 3 --execute --agent hermes --json
python3 ~/github/go-workflow-stack/cli/go.py loop <target-repo> --max-tasks 10 --json
python3 ~/github/go-workflow-stack/cli/go.py go-loop <target-repo> --max-tasks 10 --json  # explicit alias
```

Normalize user-facing first tokens with `/^go+$/i`: `go`, `GO`, `Go`, `GOO`, `gOo`, etc. all mean: invoke the repo-local go router. Normalize `loop`, `go-loop`, and `goloop` to the stronger `go-loop` route. The router inspects: repo exists, `.go/project.json`, vision, principles, hierarchy, open/active/blocked/done task counts, validity, and then recommends `spike`, `auto`, `go-loop`, or task creation. For `spike`, it distinguishes `mode=create_repo` from `mode=repair_existing_repo`; repair examples include `--skip-repo-complete` to avoid overwriting mature repos.

`go spike` is the bootstrap command Viggo can say when the project is still only an idea/design:

1. Resolve target: existing repo if named/found; otherwise create a new repo directory and initialize Git.
2. Apply repo-complete basics without overwriting existing files.
3. Write `.go/vision.json` from the rough intent/design.
4. Write `.go/architecture-principles.json` with durable constraints.
5. Write `.go/hierarchy.json` epics and `.go/tasks/open/*.json` in execution order.
6. Append an ADR-lite decision event that this repo now uses the go spike/go auto contract.
7. Validate and report the next open task.

`go auto` is the autonomous continuation command. It means Viggo hands control to the agent inside repo-local safety rails; it is not just task-list printing and it is not a request for Viggo to keep typing the next phase. Bare `go` in a repo-local project should converge to this same command-train behavior after routing.

**No-command-spam rule:** when Viggo says `go`, `go auto`, or `go-loop` in a repo-local context, the invoking coding agent must start the tool-call train immediately. Do not reply with a list of commands for Viggo to run. The default action is: inspect route/status, repair/confirm the `.go` contract, create or claim the next task, execute inside scope, verify, critic/recheck, repair, finish with evidence, and continue until done/repository-gate/budget.

**Task-first invariant:** every non-empty new GO instruction becomes a new repo-local task before product execution, even when other open tasks already exist. Invoke `go <repo> --intent "<instruction>" --write [--loop] --execute`; this must append `task.created`, then claim the task before any build adapter or product diff. Use direct `go-loop` only to continue already-materialized open tasks. Direct loop execution with no open task fails closed.

**Separate-message provenance invariant:** when Viggo sends a substantial instruction and later sends a standalone `GO`, use the exact earlier message text as `--intent` and pass a durable `--intent-source-ref` (Telegram message reference when available; otherwise a stable Hermes session/message reference). The created task must store the text, SHA-256, and source reference in `intent_source`, and the `task.created` event must repeat the hash/reference. Do not silently substitute a chat summary or inferred paraphrase.

**Requested-outcome closure invariant:** intent-created tasks track every semantic requirement as `R1`, `R2`, etc. Before finish, record each item with `task outcome <repo> --task-id <id> --outcome R# --status verified|blocked|rejected --evidence "<proof>"`. General task evidence does not replace per-R# evidence. Manual finish and `go-loop --execute` must fail closed while any item is pending or lacks evidence; 7/8 is blocked, 8/8 can finish.

Ask only when the emitted preflight reports an active repository gate, external authority is required, the outcome is genuinely ambiguous, or a real scope/product tradeoff needs direction. Do not inject inactive gate scenarios into every ordinary handoff. Everything else is agent work.

Before implementation, the agent must ensure the repo-local contract is good enough to execute:

1. Vision/end goal exists in `.go/vision.json`.
2. Design principles exist in `.go/architecture-principles.json`.
3. Hierarchy exists in `.go/hierarchy.json`.
4. A concrete task exists for the current slice.
5. Acceptance and verification evidence are explicit.

When an agent receives this contract, it must immediately continue with tool calls in the same run unless a stop condition is already present. Its `execution_policy` is deliberately high-autonomy: do not ask when a safe default exists; create same-scope follow-up tasks when verification/self-reflect proves they are needed; continue after self-reflect or escalate to `go-loop` when the work is still not genuinely done. Its `run_envelope` adds machine-readable preflight, budget, per-command timeout, run-until condition, checkpoint triggers, quiet output policy, and expected result schema. Every adapter receives `GO_CONTEXT_JSON` with vision, principles, hierarchy, task, evidence, and decisions. The semantic critic is enabled by default; `--no-semantic-critic` is the explicit escape hatch. Adapter hooks remain available through `--build-command`, `--critic-command`, `--repair-command`, and `--repair-agent codex|hermes`. Each attempt writes `.go/runs/<task-id>/attempt-XX/` artifacts and `.go/runs/latest.json` resume state.

1. Run route/status/contract/dirty validation.
2. Create or claim one task at a time: next/create → claim → execute → verify.
3. Run recheck/devil/critic; first green is not done for non-trivial work.
4. Repair in scope and re-run verification when review finds blockers.
5. If `go auto` discovers new same-scope work, a bug from live proof, or Viggo corrects behavior mid-run, create and claim a concrete `.go/tasks/open/<id>.json` task before patching. Do not treat ad-hoc fixes as outside the repo-local workflow just because they are obvious.
6. Finish only with evidence appended to `.go/evidence/events.jsonl`.
7. Commit/push according to repo policy when the task changes repo files.
8. After the task batch, run self-reflect: decide whether vision/principles/tasks/skills need improvement.
9. If self-reflect, failed review, weak first-green, or remaining same-scope work requires continued repair, continue or invoke `go loop`.
10. Summarize to Viggo compactly only at done/blocker/checkpoint; no Telegram command spam.
11. Convert Viggo's next feedback into new `.go` tasks/decisions, then repeat on the next `go`/`go auto`.

`go loop` is the stronger control-handoff contract: continue selecting, claiming, executing, verifying, repairing, and creating same-scope follow-up tasks until done, budget exhausted, or blocker. Use it when Viggo says or implies: loop, werk tot groen, ga door, controle afgeven, avondrun, or when `go auto` discovers it should not stop at the first batch.

Stop conditions: blocking dirty state in owned scope, merge conflict, secret-looking/destructive/public/payment action, missing credentials, or genuinely ambiguous recipient/outcome.

For v0.2+ authoring and handoff, prefer CLI primitives over hand-written `.go` JSON:

```bash
python3 ~/github/go-workflow-stack/cli/go.py adopt <target-repo> --project-id <id> --name "<name>"
python3 ~/github/go-workflow-stack/cli/go.py status <target-repo> --json
python3 ~/github/go-workflow-stack/cli/go.py epic create <target-repo> --id <id> --title "<title>"
python3 ~/github/go-workflow-stack/cli/go.py task create <target-repo> --id <id> --summary "<summary>" --epic <epic-id>
python3 ~/github/go-workflow-stack/cli/go.py task create <target-repo> --id <id> --summary "<summary>" --feature <epic.feature>
python3 ~/github/go-workflow-stack/cli/go.py decision create <target-repo> --id adr-001 --title "<title>" --context "<why>" --decision "<what>"
python3 ~/github/go-workflow-stack/cli/go.py bundle export <target-repo> --output /tmp/project.go-bundle.json
python3 ~/github/go-workflow-stack/cli/go.py bundle import <review-repo> /tmp/project.go-bundle.json --write --agent hermes --task-id import-review
```

Use `adopt` only when the target repo does not already have `.go/` state; it refuses existing non-empty `.go/` directories unless `--force` is explicitly passed. Use `epic create`, `task create`, and `decision create` for normal follow-up authoring so Hermes does not hand-write repo-local hierarchy/task/ADR JSON. Use `bundle export/import` for clone-safe handoffs: import is dry-run unless `--write`, and write mode stores `.go/imports/<bundle_id>.json` plus a decision event without overwriting existing target state.

For a brand-new public repo, use GitHub's template flow from `go-project-template` when practical. For an existing repo, copy only `.go/` and keep edits scoped.

## When creating a Vision Brief

Include these boundaries:

- **North Star:** a fresh clone can explain and operate its own agent contract.
- **Wedge:** reusable workflow tooling, repo-local project state; not a central vault task database.
- **Principles:** repo-local SSOT, JSON/JSONL canonical, small files, scoped safety, agent-readable first.
- **Non-goals:** no broad migration, no unattended daemon/dashboard/cross-project orchestration, no Markdown as canonical, no central AW Lite rebuild under a new name.
- **First proof:** schema + CLI spike: `.go/` init/validate, next/claim/finish, evidence append, fresh-clone readback, dirty/lock policy matrix.

## When creating a go-plan / spike plan

Do **not** turn this into a broad migration. Create a narrow proof chain:

1. Define `.go/` schema contracts and fixtures.
2. Implement `go init` / `go validate` clone-local smoke.
3. Implement repo-local `go next` / `go claim` / `go finish` lifecycle smoke.
4. Implement scoped dirty/lock classifier and smoke matrix.
5. Run one pilot readback and document the migration boundary.

If no dedicated tooling repo is found during an early proof, use the current workflow infrastructure for planning-state only; but when Viggo explicitly frames the deliverable as a reusable stack plus project/template repo, materialize those sibling repos instead of stopping at a vault-contained spike.

## Stack + template repo split

For public or reusable repo-local workflow work, prefer this deliverable shape:

- **Stack repo:** reusable CLI, schemas, validators, fixtures, docs, checks, releases, and this Hermes skill.
- **Template repo:** copyable minimal `.go/` project state, synthetic examples, local check, marked as a GitHub template when public.

Cross-repo coordination must name each real repository path/remote and operate through that repository's own `.go` contract. Use clone-safe bundles or explicit references for handoff; never mirror execution state into a vault. Proof must include public metadata when requested, releases, and fresh-clone validation across the pair.

## Dirty / lock policy

Replace clean-repo dogma with scoped safety:

| State | Default behavior |
|---|---|
| Unrelated dirty file | continue, report-only |
| Dirty file inside owned/modify scope | block or require explicit takeover |
| Active lock by another agent | block |
| Stale lock | inspect and use documented reclaim path |
| Merge conflict | block |
| Secret-looking or destructive change | block / require human gate |
| Generated workflow corruption | block until classified |

Do not remove locking entirely. The correction is scoped safety, not no safety rails.

## Verification targets

A first proof is not done until it shows:

- `.go/` contract validates without vault access;
- `go next` finds claimable work from repo-local JSON only;
- `go claim` and `go finish` mutate repo-local state and append evidence;
- fresh-clone/readback can summarize vision, principles, hierarchy, open work, and evidence history from repo files alone;
- dirty/lock smoke matrix proves unrelated dirt does not block while real conflicts do.

## Pitfalls

- Do not rebuild central AW Lite under another name.
- Do not migrate all existing AW Lite state before one pilot proves the shape.
- Do not conflate project architecture principles with workflow rules.
- Do not collapse vision, hierarchy, task state, and evidence into one giant JSON file.
- Do not let implementation tasks violate the Vision non-goals just because they are technically easy.
