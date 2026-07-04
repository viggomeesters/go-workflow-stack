# `$go-*` Bridge Status

This document is the practical status line for moving from vault-first AW Lite toward repo-local `.go/` project execution.

## Current

- `$go-plan` and `$go-goal` still exist as Hermes skills in `viggo-agent-skills`.
- Normal/vague `$go-plan` and `$go-goal` invocations remain AW Lite/vault-first.
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
| `aw-lite-fallback` | No repo-local `.go` project contract exists | Use AW Lite/vault planning/execution. |

## Target

- `$go-*` remains the user-facing command family.
- The stack owns protocol/tooling/validation.
- Project repos own their `.go/` state.
- The vault remains memory/index/control-plane fallback, especially for multi-repo orchestration.

## Non-goals for this bridge

- No broad migration of existing AW Lite plans/tasks.
- No full rewrite of `$go-plan`/`$go-goal`.
- No cross-repo orchestration model inside `.go` yet.
- No hidden central database under a new name.

## Smoke proof

The bridge is considered green when:

1. `go-project-template` routes as `repo-local`.
2. A temp repo without `.go/project.json` routes as `aw-lite-fallback`.
3. `scripts/apply-template.sh <target-repo>` copies `.go/` into a temp repo and validates it.
4. `make check` passes in `go-workflow-stack`.
5. Repo-complete validates the stack repo as public-ready.
