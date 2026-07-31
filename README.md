# Go Workflow Stack

![Go Workflow Stack hero](assets/hero.svg)

Reusable tooling for repo-local agentic engineering.

The stack contains schemas, validators, fixtures, and a small CLI for projects that keep their own `.go/` JSON/JSONL state next to the code.

For Viggo, `Go` is the single public repository-work command. `Go plan`,
`Go T123`, and `Go loop 2h` are modifiers; the CLI commands documented below
are internal primitives used by agents, scripts, and tests.

Projects may combine `required_stack_version` with an immutable `stack_ref` (`vX.Y.Z` or a full commit SHA). The minimum version protects compatibility; the ref makes bootstrap and cross-machine continuation reproducible.

## Why this exists

Agent work should be clone-readable. A future agent should be able to inspect a repository and understand its project state without needing a central vault task database.

## Repository roles

- **This repo (`go-workflow-stack`)**: reusable workflow tooling.
- **Template repo ([`go-project-template`](https://github.com/viggomeesters/go-project-template))**: starter `.go/` project-state structure.
- **Project repos**: own their `.go/` state and evidence.

For the full practical architecture and application flow, see [`docs/practical-architecture.md`](docs/practical-architecture.md). For the user-facing go/GO/GOO command router, see [`docs/go-command-router.md`](docs/go-command-router.md). For the current `$go-*` bridge status, see [`docs/go-bridge-status.md`](docs/go-bridge-status.md). For v0.2+ authoring commands, see [`docs/authoring-primitives.md`](docs/authoring-primitives.md). For clone-safe bundle handoffs, see [`docs/export-import-bundles.md`](docs/export-import-bundles.md). Versioned state upgrades and agent integrations are documented in [`docs/contract-migrations.md`](docs/contract-migrations.md) and [`docs/agent-adapter-protocol.md`](docs/agent-adapter-protocol.md).

Routing rule: a target repo must own a valid `.go/project.json` before workflow execution starts. Repositories without that contract fail closed and must use `adopt` or `spike`; a vault is never an execution fallback.

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

Or install the standalone console command; its wheel carries the schemas and minimal fixture and needs no sibling checkout at runtime:

```bash
uv tool install --from . go-workflow-stack
go-workflow version --json
go-workflow validate /path/to/project
```

Run the CLI against a repo-local `.go/` project:

```bash
python3 cli/go.py validate ../go-project-template
python3 cli/go.py readback ../go-project-template
python3 cli/go.py next ../go-project-template
```

Check the public template/stack pairing explicitly:

```bash
python3 cli/go.py template-check ../go-project-template --json
```

Initialize a fresh repo with the current minimal fixture:

```bash
python3 cli/go.py init ../my-project --force
```

Apply the public template's `.go/` structure to an existing repo:

```bash
GO_PROJECT_BRIEF="What this project must achieve" bash scripts/apply-template.sh ../my-project
```

The apply command validates the paired template and then creates a project-specific `.go` identity, vision, hierarchy, and task set. It does not leave the target pretending to be `go-project-template`.

## CLI commands

- `router <repo> --command GOO --intent <text>`: normalize `go`/`GO`/`GOO`/`gOo`, inspect repo state, and recommend direct handling, `spike`, `auto`, `go-loop`, or task creation. Missing repos are `mode=create_repo`; existing repos without `.go` are `mode=repair_existing_repo`.
- `spike <repo> --brief <text> [--task-scope code|docs]`: create/adopt a repo, scaffold repo-complete basics, write `.go` vision/principles/epics/tasks, and validate. `code` is the default so generated tasks include CLI/test scope for implementation repos.
- `auto <repo>`: hand off control for autonomous execution. It is the machine-readable shape behind Viggo saying bare `go` in a repo-local project: validate the durable contract, claim the next task, execute, verify, critic, repair, ship, finish, reflect, and continue until done, a repository gate, or budget. Add `--emit-handoff` for an agent handoff; add `--execute` for direct execution. Tasks with `execution_mode: agent` select Codex first and Hermes second by default; `mechanical` tasks only run declared commands. Agent work uses a write-scoped ephemeral session, followed by a separate read-only deep critic. Generic acceptance is rejected before claim and the built-in semantic critic remains enabled as a structural backstop.
- `go-loop <repo>` / `loop <repo>`: stronger Ralph/Oh-My-Codex-style control-handoff loop; continue selecting/claiming/repairing tasks until done, budget, or blocker.
- `go <repo> --intent <text> --intent-source-ref <ref> --write`: materialize the exact instruction as `intent_source.text` plus SHA-256 and an optional durable origin reference. For a later standalone Telegram `GO`, pass the preceding message text unchanged and reference that message/session; do not reconstruct the intent from a summary.
- `task outcome <repo> --task-id <id> --outcome R1 --status verified|blocked|rejected --evidence <proof>`: close one requested outcome. Tracked tasks cannot finish until every R# has a terminal disposition and non-empty evidence; autonomous execution returns `blocked` and leaves the task active when closure is incomplete.
- `adopt <repo>`: create real repo-local `.go/` project, principles, vision, and hierarchy state from CLI arguments.
- `status <repo> [--json]`: summarize route, project, task counts, next work, and dirty state.
- `doctor <repo> --platform wsl --agent hermes`: verify Python 3.11+, Git, Bash, Make, uv, agent availability, `.go` validity, and the project's minimum stack-version contract.
- `migrate <repo> [--apply]`: plan a versioned `.go` migration without writes, or explicitly apply and validate it.
- `adapter validate-result <result.json> --phase <phase>`: fail-closed validation for the shared Codex/Hermes/custom adapter result protocol.
- `proof validate <proof.json> [--evidence-root dir] [--copy-to path]`: validate live Hermes evidence, optionally recompute raw-result hashes, and copy only after all proof gates pass.
- `stack update <repo> --to vX.Y.Z [--apply]`: resolve and verify an immutable stack tag, show a dry-run by default, and apply atomically with rollback data only when explicitly requested.
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
- `route <repo> [--json]`: classify a valid `.go` target as `repo-local`; otherwise return `missing-local-contract` with a non-zero exit and an `adopt` command.
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
```

JSON is canonical for current state. JSONL is canonical for lifecycle, evidence, and decision streams. Markdown is a human view only. Import bundles are review artifacts: they never overwrite existing project state unless a later explicit task chooses to reconcile them.

Process locks live under `.git/go-workflow-locks` so they do not dirty Git state; non-Git fixtures use `.go/locks` as a fallback.

Every executed run writes `.go/runs/latest.json` plus `.go/runs/resume.sh`. The JSON preserves effective budgets, adapters, critic, and ship flags as structured `resume_args`; the portable shell entrypoint resolves `GO_STACK` or a conventional sibling/home checkout at resume time. A campaign can therefore move between macOS and WSL without retaining the originating machine's absolute stack path.

Set `GO_EXECUTOR_AGENT=hermes` on a Hermes-first machine. An explicit `--executor-agent` still wins for a single run:

```bash
export GO_EXECUTOR_AGENT=hermes
python3 cli/go.py go-loop ../my-project --execute --agent hermes
```

Hosted CI is deliberately not part of this project. Run the complete Linux/WSL contract locally; it selects an installed Python 3.11+ or uses `uv`, executes the full suite, and verifies the stack/template pairing:

```bash
bash scripts/check-linux.sh
```

A live-model acceptance is deliberately opt-in and never reports success when Hermes is absent:

```bash
GO_RUN_REAL_HERMES_E2E=1 bash scripts/run-hermes-acceptance.sh
```

Run the standalone distribution check and the Python/frontend/existing-repo pilots:

```bash
bash scripts/check-pilots.sh
```

## Development

Use local validation before committing or publishing changes. The check compiles the Python CLI where applicable and validates the template repository contract.

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest tests/test_smoke.py -q
make check
bash scripts/check.sh
```

Install the committed launcher as the root-managed release trust anchor. Do not treat the mutable repo copy as its own security proof:

```bash
set -euo pipefail
RELEASE_REPO="$PWD"
RELEASE_COMMIT="$(
  /usr/bin/env -i HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 \
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1 \
    GIT_NO_REPLACE_OBJECTS=1 \
    /usr/bin/git --no-replace-objects -C "$RELEASE_REPO" rev-parse --verify 'HEAD^{commit}'
)"
RELEASE_LAUNCHER="$(/usr/bin/mktemp /tmp/go-release-launcher.XXXXXX)"
trap '/bin/rm -f "$RELEASE_LAUNCHER"' EXIT
/usr/bin/env -i HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 \
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1 \
  GIT_NO_REPLACE_OBJECTS=1 \
  /usr/bin/git --no-replace-objects --no-pager -C "$RELEASE_REPO" \
  show "$RELEASE_COMMIT:scripts/release-check.sh" >"$RELEASE_LAUNCHER"
sudo /usr/bin/install -o root -g root -m 0755 \
  "$RELEASE_LAUNCHER" /usr/local/bin/go-workflow-release-check
```

Run the pre-tag candidate gate locally without publishing or invoking hosted automation:

```bash
/usr/local/bin/go-workflow-release-check --repo "$PWD" 0.3.8 --allow-candidate
```

After committing and creating the annotated tag, run the mandatory final gate. It is intentionally local, can run before the release commit or tag is pushed, requires a clean tree, and tests the archived tag payload rather than the mutable checkout:

```bash
/usr/local/bin/go-workflow-release-check --repo "$PWD" 0.3.8
```

If a later repair commit must revalidate an existing immutable tag, opt in explicitly:

```bash
/usr/local/bin/go-workflow-release-check --repo "$PWD" 0.3.8 --validate-existing
```

Existing-tag validation proves that the tag is an ancestor of `HEAD`. A shallow checkout is hydrated in an isolated temporary clone from the canonical HTTPS `viggomeesters/go-workflow-stack` origin so the working checkout remains untouched and historical tag regressions remain available. The historical stack payload never pairs with an adjacent or explicitly supplied project template: `--validate-existing` refuses `GO_PROJECT_TEMPLATE`, because executing a historical gate against external mutable state would violate checkout isolation. When a historical release requires the public template suite, the validator uses an explicit release-to-template commit mapping fetched from the canonical HTTPS template origin; v0.3.8 is paired with `go-project-template` commit `3956fc92f9e99520756d10f08373635182f22d67`. Validate current template compatibility separately against the current stack. An isolated `/usr/bin/python3 -I` launcher builds a minimal environment before Bash starts, so inherited shell functions and startup files cannot affect path resolution. When installed outside the repository, the launcher rejects untrusted origins before executing repository code; existing-tag mode also proves the selected `HEAD` is reachable from an origin branch and loads the inner validator from that isolated remote clone. Release tools must resolve from the root-managed system path `/usr/local/bin:/usr/bin:/bin`; install `uv` there for historical distribution checks instead of relying on a user-writable `~/.local/bin`. Release modes are selected only through explicit command-line flags, not inherited shell variables. SSH origins, arbitrary forks, remote helpers, Git replacement refs, Git config injection, Git hooks inherited through environment config, and transport proxy/SSL overrides are rejected or ignored. Local filesystem remotes are accepted only for fixtures with `--allow-local-origin`.

Review and explicitly apply an immutable project stack update:

```bash
go-workflow stack update . --to v0.3.8 --json
go-workflow stack update . --to v0.3.8 --apply --json
```

## Privacy and security

The repository should contain only synthetic public fixtures. Do not commit private vault data, credentials, customer data, local runtime DBs, or generated machine state.

## Status

Public v0.3.8 release. GO runs now plan builder/reviewer capacity from task scope, keep work completion separate from review approval, and require attributed finish evidence. Subscription usage remains explicitly distinct from provider invoice cost.

## License

This project is released under the MIT License. See [`LICENSE`](LICENSE) for the full license text.
