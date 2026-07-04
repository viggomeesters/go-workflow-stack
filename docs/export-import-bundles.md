# Export / Import Bundles

Repo-local `.go` state should be inspectable and movable without the vault. The bundle commands provide that bridge without making imports destructive.

## Export

```bash
python3 cli/go.py bundle export <repo> --output /tmp/project.go-bundle.json
```

The export validates the source repo first, then writes a compact JSON bundle containing:

- source project id/name
- readback summary: north star, wedge, principles, feature groups, next task
- task counts and open/active/blocked task records
- recent runs/evidence/decision events

Done tasks are counted by default. Add `--include-done` when a handoff needs done task summaries and evidence.

## Import / reconcile

```bash
python3 cli/go.py bundle import <target-repo> /tmp/project.go-bundle.json
python3 cli/go.py bundle import <target-repo> /tmp/project.go-bundle.json --write --agent hermes --task-id import-review
```

Default import is a dry run. It validates the target `.go` state and the bundle, then prints the planned target path.

`--write` stores the bundle under:

```text
.go/imports/<bundle_id>.json
```

It also appends a `decision.recorded` event. It does **not** overwrite `project.json`, `vision.json`, `hierarchy.json`, task files, or evidence streams from the target repo. Reconciliation remains an explicit later task.

## Routing boundary

When `<repo>/.go/project.json` exists, repo-local `.go` state wins. AW Lite/vault is fallback/control-plane only. This prevents the old failure mode where a repo-local task request created an unrelated vault task.

## Verification

Run:

```bash
make check
python3 cli/go.py bundle export fixtures/minimal --output /tmp/go-bundle.json
python3 cli/go.py bundle import fixtures/minimal /tmp/go-bundle.json
```

For a write smoke, import into a temporary adopted repo and then run `python3 cli/go.py validate <repo>`.
