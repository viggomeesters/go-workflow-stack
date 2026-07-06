---
name: repo-local-agent-workflow
description: "Use when shaping, planning, or implementing self-contained repo-local agent workflow systems: go-workflow-stack tooling, go-project-template adoption, .go contracts, JSON/JSONL project state, repo-local task lifecycle, scoped dirty/lock policy, or migration away from central vault/AW Lite execution state."
version: 1.1.0
author: Hermes Agent
license: MIT
user-invocable: false
triggers:
  - repo-local go workflow
  - repo-local agent workflow
  - self-contained agentic engineering
  - .go contract
  - go-workflow-stack
  - go-project-template
  - schema + CLI spike
  - AW lock
  - dirty repo dogma
  - vault-first task state
metadata:
  hermes:
    tags: [agent-workflow, repo-local, jsonl, planning, cli, schemas, dirty-state]
    related_skills: [go-vision, go-plan, agent-workflow-lite, grill-me]
---

# Repo-local Agent Workflow

## Overview

Use this skill for the class of work where Viggo is designing or implementing a self-contained agent workflow contract inside each project repo, rather than operating ordinary vault-first Agent Workflow Lite state.

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

For v0.3+ end-to-end command routing, use these higher-level primitives:

```bash
python3 ~/github/go-workflow-stack/cli/go.py router <target-repo> --command GOO --intent "<rough Viggo input>" --json
python3 ~/github/go-workflow-stack/cli/go.py spike <target-repo> --brief "<rough intent>"
python3 ~/github/go-workflow-stack/cli/go.py auto <target-repo> --max-tasks 3 --json
python3 ~/github/go-workflow-stack/cli/go.py loop <target-repo> --max-tasks 10 --json
python3 ~/github/go-workflow-stack/cli/go.py go-loop <target-repo> --max-tasks 10 --json  # explicit alias
```

Normalize user-facing first tokens with `/^go+$/i`: `go`, `GO`, `Go`, `GOO`, `gOo`, etc. all mean: invoke the repo-local go router. The router inspects: repo exists, `.go/project.json`, vision, principles, hierarchy, open/active/blocked/done task counts, validity, and then recommends `spike`, `auto`, `go-loop`, or task creation.

`go spike` is the bootstrap command Viggo can say when the project is still only an idea/design:

1. Resolve target: existing repo if named/found; otherwise create a new repo directory and initialize Git.
2. Apply repo-complete basics without overwriting existing files.
3. Write `.go/vision.json` from the rough intent/design.
4. Write `.go/architecture-principles.json` with durable constraints.
5. Write `.go/hierarchy.json` epics and `.go/tasks/open/*.json` in execution order.
6. Append an ADR-lite decision event that this repo now uses the go spike/go auto contract.
7. Validate and report the next open task.

`go auto` is the autonomous continuation command. It means Viggo hands control to the agent inside repo-local safety rails; it is not just task-list printing. When Hermes/Bertus receives this contract, it must immediately continue with tool calls in the same run unless a stop condition is already present:

1. Run status/route/dirty validation.
2. Take one open task at a time: next → claim → execute → verify.
3. Run recheck/devil/hardening before finish when the task changed code/docs/contracts.
4. Finish only with evidence appended to `.go/evidence/events.jsonl`.
5. After the task batch, run self-reflect: decide whether vision/principles/tasks need improvement.
6. If self-reflect, failed review, weak first-green, or remaining same-scope work requires continued repair, `go auto` may invoke `go loop`.
7. Summarize to Viggo compactly, max configured chars, no technical fluff spam.
8. Convert Viggo's next feedback into new `.go` tasks/decisions, then repeat on the next `go auto`.

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

The AW Lite/vault plan can coordinate execution, but the proof must include real repo paths/remotes, public metadata when requested, releases, and fresh-clone validation across the pair.

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
