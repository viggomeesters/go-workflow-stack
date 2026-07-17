# `$go-*` Bridge Status

This document is the practical status line for repo-local `.go/` project execution.

## Current

- `$go-*` commands resolve an explicit repository and use its local `.go` contract.
- Existing repositories without `.go/project.json` fail closed and must be adopted or spiked before execution.
- `repo-local-agent-workflow` is sourced from `go-workflow-stack` and loaded into Hermes by symlink.
- `go-project-template` is the GitHub template for new project-local `.go/` state.
- `bundle export/import` moves compact `.go` state between clones/review contexts without vault access; import is dry-run unless `--write` stores `.go/imports/<bundle_id>.json`.

## Bridge

`go-workflow-stack` now has a route detector:

```bash
python3 ~/github/go-workflow-stack/cli/go.py route <target-repo> --json
```

Route meanings:

| Mode | Meaning | Default action |
|---|---|---|
| `repo-local` | `<target-repo>/.go/project.json` exists | Use stack `.go` commands for project-local tasks. |
| `missing-local-contract` | No valid repo-local `.go` project contract exists | Stop execution and run the returned `adopt` command, or use `spike`. |

## Target

- `$go-*` remains the user-facing command family.
- The stack owns protocol/tooling/validation.
- Project repos own their `.go/` state.
- Cross-repo work references explicit repo-local contracts and bundles; it does not create a hidden central execution database.

## Non-goals for this bridge

- No broad migration of existing AW Lite plans/tasks.
- No migration of historical workflow records as part of routing.
- No cross-repo orchestration model inside `.go` yet.
- No hidden central database under a new name.

## Smoke proof

The bridge is considered green when:

1. `go-project-template` routes as `repo-local`.
2. A temp repo without `.go/project.json` returns `missing-local-contract`, a non-zero exit, and an `adopt` command.
3. `scripts/apply-template.sh <target-repo>` copies `.go/` into a temp repo and validates it.
4. `make check` passes in `go-workflow-stack`.
5. Repo-complete validates the stack repo as public-ready.
