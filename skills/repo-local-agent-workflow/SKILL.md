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
  architecture/           # optional conditional architecture lane
    briefs/*.json          # scoped context and measurable quality attributes
    events.jsonl           # classification/review/conformance/deviation/waiver events
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

## Conditional architecture lane

Use the architecture lane only when a task changes boundaries, contracts, data ownership, integrations, security/privacy, migrations, difficult-to-reverse platform direction, or measurable system qualities. Repositories without `.go/architecture/` remain backward-compatible; tasks with explicit `architecture` metadata activate the lane immediately.

Canonical split:

- `.go/architecture/briefs/<scope-id>.json` owns scoped context, stakeholders, constraints, risks, and measurable quality attributes;
- `.go/decisions/events.jsonl` remains the only decision ledger;
- `.go/architecture/events.jsonl` records classification, review, conformance, deviations, and time-bounded waivers;
- `.go/evidence/events.jsonl` remains the verification proof stream.

Run `architecture classify` before claim for consequential tasks. A deterministic minimum may raise impact but never lower a hard signal. Material and foundational tasks require at least one exact accepted governing decision resolved from the task or an applicable brief, plus passing conformance for every scope; reuse an existing accepted decision when it already governs the change instead of manufacturing a duplicate ADR. Foundational tasks additionally require named human approval with evidence. Automation identities cannot claim human authority. Waivers require an actor, reason, accepted risk, and future timezone-aware expiry. Do not backfill historical architecture ceremony: activate incrementally at the next consequential change. Full operator commands and migration rules live in `docs/architecture-lane.md`.

## Proportionate task design review

Before calling a substantial task implementation-ready, read its actual sources
and assess observable before/after behavior, states/transitions and edge cases,
boundaries, dependencies and matching proof. Distinguish accepted rules, proposals,
bounded delegated tuning and unresolved material decisions with owner and gate.
A structurally valid task or successful check is not a semantic readiness verdict.

Record `ready`, `design_research_first` or `blocked_on_material_decision` with
specific reasons, inspected references and next action in existing task/run review
evidence or the phase result. A label, keywords or self-attestation is insufficient.
Do not add a parallel approval system. Small reversible fixes need only their
concrete change and proportionate verification; delegated tuning remains autonomous.

Use existing architecture decision/scope references for governing prerequisites:
explicit references must resolve to accepted records even for none/local impact.
Use opted-in lifecycle dependencies for research-before-implementation ordering;
legacy prose or arbitrary dependency metadata does not enforce execution order.
Research tasks may intentionally explore open choices within research-only scope.
Their completion cannot stand in for the original requested implementation/fix.

Critics inspect evidence for each original outcome. An unknown root cause or
unreproduced bug is not fixed; keep that R# pending/blocked until correction proof
exists. Use runtime, visual, motion, audio and reference evidence only when relevant.
Pre-publication critics still leave future shipping receipts to the controller.
The runtime carries the same rubric in `task_design_review`; it is instructions,
not an automatically granted readiness verdict. See `docs/task-design-readiness.md`
for the audit, compatibility boundary and paired examples.

## Bounded semantic intake

For a substantial or unclear rough request, use `intake explore` before claim
instead of treating syntax, keywords or a schema-valid placeholder as semantic
task design. Preserve the exact intent/source and all `O#` outcomes. Let the
read-only controlled adapter inspect current code, tasks, vision and latest
decision states, then require explicit `create`, `reuse` or `update` actions.

Deterministic validation proves transport, bindings, authority, persistence and
atomicity only; the model/reviewer owns the content judgment. Unknown bug causes
become research. Superseded decisions stay superseded. Advice/planning may write
durable assessment/tasks but cannot authorize claim, and unresolved questions
block only the affected tasks. Never rewrite vision or the decision ledger from
an intake assessment. Repeated identical intake is idempotent; later feedback
updates an open task without erasing earlier outcomes, sources, acceptance or
verification. See `docs/autonomous-intake.md`.

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
python3 ~/github/go-workflow-stack/cli/go.py intake explore <target-repo> --intent "<exact request>" --source-ref "<origin>" --authority planning --executor-agent codex --model gpt-6-astra --effort high --write --json
python3 ~/github/go-workflow-stack/cli/go.py auto <target-repo> --max-tasks 3 --json
python3 ~/github/go-workflow-stack/cli/go.py auto <target-repo> --max-tasks 3 --emit-handoff --json
python3 ~/github/go-workflow-stack/cli/go.py auto <target-repo> --max-tasks 3 --execute --agent hermes --json
python3 ~/github/go-workflow-stack/cli/go.py loop <target-repo> --max-tasks 10 --json
python3 ~/github/go-workflow-stack/cli/go.py go-loop <target-repo> --max-tasks 10 --json  # explicit alias
```

Normalize user-facing first tokens with `/^go+$/i`: `go`, `GO`, `Go`, `GOO`, `gOo`, etc. all mean: invoke the single public repo-local Go router. Treat `Go plan`, `Go T123`, and `Go loop 2h` as modifiers, then report `selected_route` and continue through the internal primitive without another user command. The router inspects: repo exists, `.go/project.json`, vision, principles, hierarchy, open/active/blocked/done task counts, validity, and then selects discovery, planning, task intake, execution, or a bounded loop. For `spike`, it distinguishes `mode=create_repo` from `mode=repair_existing_repo`; repair examples include `--skip-repo-complete` to avoid overwriting mature repos.

`go spike` is the bootstrap command Viggo can say when the project is still only an idea/design:

1. Resolve target: existing repo if named/found; otherwise create a new repo directory and initialize Git.
2. Apply repo-complete basics without overwriting existing files.
3. Write `.go/vision.json` from the rough intent/design.
4. Write `.go/architecture-principles.json` with durable constraints.
5. Write `.go/hierarchy.json` epics and `.go/tasks/open/*.json` in execution order.
6. Append an ADR-lite decision event that this repo now uses the go spike/go auto contract.
7. Validate and report the next open task.

`auto` is an internal autonomous continuation primitive. Viggo hands control to the agent with canonical `Go`; the router chooses `auto` or the stronger loop internally. It is not a request for Viggo to keep typing the next phase.

**No-command-spam rule:** when Viggo says `Go` or uses a modifier such as `Go plan`, `Go T123`, or `Go loop 2h`, the invoking coding agent must start the tool-call train immediately. Do not reply with a list of internal commands for Viggo to run. The default action is: inspect route/status, report one route line, repair/confirm the `.go` contract, create or claim the next task, execute inside scope when allowed, verify, critic/recheck, repair, finish with evidence, and continue until done/repository-gate/budget. `Go plan` stops before implementation.

**Task-first invariant:** every non-empty new GO instruction becomes a new repo-local task before product execution, even when other open tasks already exist. Invoke `go <repo> --intent "<instruction>" --write [--loop] --execute`; this must append `task.created`, then claim the task before any build adapter or product diff. Use direct `go-loop` only to continue already-materialized open tasks. Direct loop execution with no open task fails closed.

**Separate-message provenance invariant:** when Viggo sends a substantial instruction and later sends a standalone `GO`, use the exact earlier message text as `--intent` and pass a durable `--intent-source-ref` (Telegram message reference when available; otherwise a stable Hermes session/message reference). The created task must store the text, SHA-256, and source reference in `intent_source`, and the `task.created` event must repeat the hash/reference. Do not silently substitute a chat summary or inferred paraphrase.

**Requested-outcome closure invariant:** intent-created tasks track every semantic requirement as `R1`, `R2`, etc. Before finish, record each item with `task outcome <repo> --task-id <id> --outcome R# --status verified|blocked|rejected --evidence "<proof>"`. General task evidence does not replace per-R# evidence. Manual finish and `go-loop --execute` must fail closed while any item is pending or lacks evidence; 7/8 is blocked, 8/8 can finish.

Ask only when the emitted preflight reports an active repository gate, external authority is required, the outcome is genuinely ambiguous, or a real scope/product tradeoff needs direction. Do not inject inactive gate scenarios into every ordinary handoff. Everything else is agent work.

Before implementation, the agent must ensure the repo-local contract is good enough to execute:

1. Fetch tags in the canonical `go-workflow-stack` checkout and run `stack update <repo> --latest --apply` before route, task creation, claim, or product edits. The highest annotated immutable release wins; current pins no-op. Failure or overlapping dirty `.go` migration state blocks product work. Never use mutable `main` or `GO_STACK_ALLOW_DEV=1` as an update shortcut.
2. Vision/end goal exists in `.go/vision.json`.
3. Design principles exist in `.go/architecture-principles.json`.
4. Hierarchy exists in `.go/hierarchy.json`.
5. A concrete task exists for the current slice.
6. Acceptance and verification evidence are explicit.

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


## Explicit task lifecycle and adoption

Released through v0.3.24: `.go/project.json` may explicitly define execution
(default model/effort, workspace, release, optional phase profiles), publication
and deployment policies. New intake resolves these defaults plus task overrides.
Task creation, intent, execution brief, recommendation promotion, follow-up and
configured scaffolding retain the same contract and pending acceptance coverage.
A stack pin update does not adopt lifecycle policy. Use `migrate --lifecycle` for
a preview, `--config SETTINGS --apply` for explicit quiescent adoption, and the
returned journal with `--resume` or `--rollback` for recovery. Do not start work
through a pending migration or overwrite changed task history. See
`docs/stack-updates.md` for exact settings and recovery boundaries.

Each executing phase reads the owned task scope, immutable context and current
feedback; it writes only its permitted output and records raw verification or
critic findings. The controller prepares version/changelog before final checks,
serializes integration/publication and retains exact effect identity through
recovery. Required deployment has separate authorization and authoritative live
commit/version/artifact readback. A successful worker turn or command exit alone
is not task completion. Task status, managed worker phase and outer chat status
are different records.

Handoff retains the same task/run/workspace and requested model selection through
`.go` context and resume artifacts. Never silently switch models or treat metadata
as independent effective-model attestation. Review-bundle export/import preserves
full contract/provenance records and references, but does not transfer an active
runtime or restore executable tasks. Report this distinction explicitly.

Choose relevant skills for the task's scope and risk. Required lifecycle checks
remain enforced; skill/schema lint is structural evidence and does not replace
executed checks, review, exact publication or live proof. Reuse the selected legacy
lessons recorded in `.go/plans/legacy-insights.json`, without restoring a vault
writer, broad mandatory skill chain, task-ID release versions, global skill edits
or GitHub Actions. Updating this repository-local skill source does not authorize
automatic installation or configuration changes in another runtime.
