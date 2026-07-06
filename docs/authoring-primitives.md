# Authoring Primitives

`go-workflow-stack` v0.2+ adds repo-local authoring commands so agents do not need to hand-write `.go` JSON for common work. For the sloppy user-facing `go`/`GO`/`GOO` routing layer, see [`go-command-router.md`](go-command-router.md).

## `router`

Normalize the first token and inspect repo state before choosing the next command:

```bash
python3 ~/github/go-workflow-stack/cli/go.py router <repo> --command GOO --intent "marktplaats inbox bot" --json
```

`/^go+$/i` tokens become `go`; the router then checks repo existence, `.go/project.json`, vision, principles, hierarchy, and task counts.

## `spike`

Create the missing project container from rough intent. This is the command-shape behind Viggo saying `go spike` to Bertus/Hermes:

```bash
python3 ~/github/go-workflow-stack/cli/go.py spike ~/github/marktplaats-bot \
  --project-id marktplaats-bot \
  --name "Marktplaats Bot" \
  --brief "Inbox monitor that checks Marktplaats messages and alerts Viggo" \
  --epic "inbox-monitor|Inbox Monitor" \
  --task "design-monitor|Design the inbox monitor" \
  --task "build-poller|Build the polling loop"
```

Behavior:

1. If the repo does not exist, create it and run `git init`.
2. Scaffold repo-complete basics without overwriting existing files.
3. Write `.go/project.json`, `.go/vision.json`, `.go/architecture-principles.json`, `.go/hierarchy.json`.
4. Write open tasks in the supplied order, or a default delivery loop if no tasks are supplied.
5. Append an ADR-lite decision event and validate the result.

## `auto` / `loop`

Hand off control for autonomous execution:

```bash
python3 ~/github/go-workflow-stack/cli/go.py auto ~/github/marktplaats-bot --max-tasks 3 --json
python3 ~/github/go-workflow-stack/cli/go.py loop ~/github/marktplaats-bot --max-tasks 10 --json
```

`go auto` is not a task-list printer. It means Viggo gives control to the agent inside repo-local safety rails. The agent should execute, verify, recheck/devil, finish with evidence, self-reflect, then continue or escalate.

`go auto` may invoke `go loop` when self-reflect produces follow-up tasks, review fails, first green is weak, or work should continue beyond the initial batch. `go loop` means keep selecting/claiming/repairing tasks until done, budget exhausted, or blocker.

## `adopt`

Create real repo-local project state in a repository that does not already have `.go/` state:

```bash
python3 ~/github/go-workflow-stack/cli/go.py adopt <repo> \
  --project-id my-project \
  --name "My Project" \
  --verification "npm run check" \
  --feature-group "workflow|Workflow" \
  --feature "workflow|repo-local|Repo-local workflow"
```

Safety rule: `adopt` refuses to overwrite an existing non-empty `.go/` directory unless `--force` is passed.

## `status`

Read the route, validity, task counts, next work, and scoped dirty state:

```bash
python3 ~/github/go-workflow-stack/cli/go.py status <repo>
python3 ~/github/go-workflow-stack/cli/go.py status <repo> --json
```

Use this before deciding whether a `$go-*` request should be repo-local or AW Lite fallback.

## `epic create`

Create an epic-lite work package in `hierarchy.json`:

```bash
python3 ~/github/go-workflow-stack/cli/go.py epic create <repo> \
  --id repo-local-contract \
  --title "Repo-local contract" \
  --description "Standardize vision, ADR-lite decisions, epics, tasks, and evidence"
```

Epics are deliberately lightweight: an `id`, `title`, optional `description`, optional feature list, and direct task links. This gives agents a standard large-work primitive without Jira cosplay.

## `task create`

Create an open task from CLI arguments and attach it to hierarchy when a feature or epic is provided:

```bash
python3 ~/github/go-workflow-stack/cli/go.py task create <repo> \
  --id review-public-copy \
  --summary "Review public copy" \
  --feature site-operations.repo-local-workflow \
  --read README.md \
  --modify README.md \
  --acceptance "README remains public-safe" \
  --verification "npm run check"

python3 ~/github/go-workflow-stack/cli/go.py task create <repo> \
  --id standardize-decisions \
  --summary "Standardize ADR-lite decision events" \
  --epic repo-local-contract \
  --acceptance "Decision contract is documented and validated" \
  --verification "git diff --check"
```

The command validates the repo after writing. It refuses duplicate task ids and rolls back the task file if hierarchy attachment fails.

## `decision create`

Record an ADR-lite project decision as append-only JSONL:

```bash
python3 ~/github/go-workflow-stack/cli/go.py decision create <repo> \
  --id adr-001 \
  --title "Use repo-local .go as execution SSOT" \
  --context "Agents need clone-local continuation" \
  --decision "Project workflow state lives in .go/" \
  --consequence "Vault remains memory/index only" \
  --agent hermes \
  --task-id standardize-decisions
```

This appends a `decision.recorded` event under `.go/decisions/events.jsonl`; no Markdown ADR ceremony is required for the canonical record.

## `bundle export` / `bundle import`

Move repo-local state between clones or review contexts without touching the vault:

```bash
python3 ~/github/go-workflow-stack/cli/go.py bundle export <repo> --output /tmp/project.go-bundle.json
python3 ~/github/go-workflow-stack/cli/go.py bundle import <target-repo> /tmp/project.go-bundle.json
python3 ~/github/go-workflow-stack/cli/go.py bundle import <target-repo> /tmp/project.go-bundle.json --write --agent hermes --task-id import-review
```

Import is dry-run by default. `--write` stores an immutable review artifact under `.go/imports/` and appends a decision event; it does not overwrite target tasks, vision, hierarchy, or evidence.

## Current boundary

These commands author and move single-repo `.go` state. They do not replace AW Lite multi-repo orchestration and they do not migrate historical AW Lite plans/tasks. When `.go/project.json` exists, repo-local `.go` wins; AW Lite remains fallback/control-plane.
