# Go Workflow Stack

![Go Workflow Stack hero](assets/hero.svg)

Reusable tooling for repo-local agentic engineering.

The stack contains schemas, validators, fixtures, and a small CLI for projects that keep their own `.go/` JSON/JSONL state next to the code.

## Why this exists

Agent work should be clone-readable. A future agent should be able to inspect a repository and understand its project state without needing a central vault task database.

## Repository roles

- **This repo (`go-workflow-stack`)**: reusable workflow tooling.
- **Template repo ([`go-project-template`](https://github.com/viggomeesters/go-project-template))**: starter `.go/` project-state structure.
- **Project repos**: own their `.go/` state and evidence.

For the full practical architecture and application flow, see [`docs/practical-architecture.md`](docs/practical-architecture.md). For the user-facing go/GO/GOO command router, see [`docs/go-command-router.md`](docs/go-command-router.md). For the current `$go-*` bridge status, see [`docs/go-bridge-status.md`](docs/go-bridge-status.md). For v0.2+ authoring commands, see [`docs/authoring-primitives.md`](docs/authoring-primitives.md). For clone-safe bundle handoffs, see [`docs/export-import-bundles.md`](docs/export-import-bundles.md).

Routing rule: when a target repo contains `.go/project.json`, repo-local `.go` state wins. AW Lite/vault state is fallback/control-plane only.

## Practical architecture in one minute

Use this stack when you want reusable commands and validation. Use [`go-project-template`](https://github.com/viggomeesters/go-project-template) when you want to start a new repo that already carries its own `.go/` state. A real project should copy/adapt the template and then keep tasks, evidence, decisions, and architecture principles in its own repository.

```text
go-workflow-stack  -> validates/operates -> project repo with .go/
go-project-template -> seeds/copies ------^
```

## Install / usage

Clone this repository next to a project repository:

```bash
git clone https://github.com/viggomeesters/go-workflow-stack.git
git clone https://github.com/viggomeesters/go-project-template.git
cd go-workflow-stack
make check
```

Run the CLI against a repo-local `.go/` project:

```bash
python3 cli/go.py validate ../go-project-template
python3 cli/go.py readback ../go-project-template
python3 cli/go.py next ../go-project-template
```

Initialize a fresh repo with the current minimal fixture:

```bash
python3 cli/go.py init ../my-project --force
```

Apply the public template's `.go/` structure to an existing repo:

```bash
bash scripts/apply-template.sh ../my-project
```

## CLI commands

- `router <repo> --command GOO --intent <text>`: normalize `go`/`GO`/`GOO`/`gOo`, inspect repo state, and recommend `spike`, `auto`, `go-loop`, or task creation. Missing repos are `mode=create_repo`; existing repos without `.go` are `mode=repair_existing_repo`.
- `spike <repo> --brief <text> [--task-scope code|docs]`: create/adopt a repo, scaffold repo-complete basics, write `.go` vision/principles/epics/tasks, and validate. `code` is the default so generated tasks include CLI/test scope for implementation repos.
- `auto <repo>`: hand off control for autonomous execution; includes explicit high-autonomy `execution_policy`, machine-readable `run_envelope`, same-scope follow-up creation, and escalation to `go-loop` when self-reflect/review requires continued repair. Add `--emit-handoff` to produce a Hermes/Bertus task/scope/gate/evidence handoff; add `--execute` to let the CLI run preflight → claim → verification → finish/block → reflection for verification-ready tasks.
- `go-loop <repo>` / `loop <repo>`: stronger control-handoff loop; continue selecting/claiming/repairing tasks until done, budget, or blocker.
- `adopt <repo>`: create real repo-local `.go/` project, principles, vision, and hierarchy state from CLI arguments.
- `status <repo> [--json]`: summarize route, project, task counts, next work, and dirty state.
- `epic create <repo> --title <text>`: create an epic-lite work package in `hierarchy.json`.
- `task create <repo> --summary <text> [--epic epic-id | --feature epic.feature]`: create an open repo-local task and optionally attach it to an epic or feature.
- `decision create <repo> --title <text> --context <text> --decision <text>`: append an ADR-lite `decision.recorded` event.
- `init <repo>`: create a minimal `.go/` fixture.
- `validate <repo>`: validate `.go/` JSON and JSONL files.
- `next <repo>`: show the first open task.
- `claim <task-id> --repo <repo> --agent <name>`: move an open task to active.
- `finish <task-id> --repo <repo> --agent <name> --evidence <text>`: move an active task to done and append evidence.
- `dirty-check <repo>`: classify dirty Git state against owned paths.
- `readback <repo>`: summarize the project from `.go/` only.
- `route <repo> [--json]`: classify the target as `repo-local` when `.go/project.json` exists, otherwise `aw-lite-fallback`.
- `bundle export <repo> [--output bundle.json]`: export a compact `.go` readback/task/history bundle without vault access.
- `bundle import <repo> bundle.json [--write]`: validate a bundle and, only with `--write`, store it under `.go/imports/` as a review/reconcile artifact.

## Contract

```text
.go/
  project.json
  architecture-principles.json
  vision.json
  hierarchy.json
  tasks/open/*.json
  tasks/active/*.json
  tasks/done/*.json
  tasks/blocked/*.json
  runs/*.jsonl
  evidence/*.jsonl
  decisions/*.jsonl
  imports/*.json
  locks/
```

JSON is canonical for current state. JSONL is canonical for lifecycle, evidence, and decision streams. Markdown is a human view only. Import bundles are review artifacts: they never overwrite existing project state unless a later explicit task chooses to reconcile them.

## Development

Use local validation before committing or publishing changes. The check compiles the Python CLI where applicable and validates the template repository contract.

```bash
make check
bash scripts/check.sh
```

## Privacy and security

The repository should contain only synthetic public fixtures. Do not commit private vault data, credentials, customer data, local runtime DBs, or generated machine state.

## Status

Early public spike. The goal is to prove the repo-local contract before broad migration.

## License

This project is released under the MIT License. See [`LICENSE`](LICENSE) for the full license text.
