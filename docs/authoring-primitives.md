# Authoring Primitives

`go-workflow-stack` v0.2 adds the first repo-local authoring commands so agents do not need to hand-write `.go` JSON for common work.

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

## `task create`

Create an open task from CLI arguments and attach it to hierarchy when a feature is provided:

```bash
python3 ~/github/go-workflow-stack/cli/go.py task create <repo> \
  --id review-public-copy \
  --summary "Review public copy" \
  --feature site-operations.repo-local-workflow \
  --read README.md \
  --modify README.md \
  --acceptance "README remains public-safe" \
  --verification "npm run check"
```

The command validates the repo after writing. It refuses duplicate task ids and rolls back the task file if hierarchy attachment fails.

## Current boundary

These commands author single-repo `.go` state. They do not replace AW Lite multi-repo orchestration and they do not migrate historical AW Lite plans/tasks.
