# Repository-local Go workflow

This repository develops the new JSON-first `.go/` workflow stack. Inside this repository, `.go/` is the source of truth; do not require `.go-workflow/config.yaml` and do not route through the legacy Life OS pipeline.

`Go` is the single public repository-work command. `Go plan`, `Go T123`, and
`Go loop 2h` are modifiers of that command; `auto`, `go-loop`, task creation,
claim, and finish are internal CLI primitives, not a menu the user must choose.

When the user says `Go`, uses a Go modifier, says `Next`, or asks to continue autonomously:

1. Fetch tags in the canonical stack checkout and run the stack-freshness preflight before route, claim, task creation, or product edits: `python3 cli/go.py stack update . --latest --stack-repo "$PWD" --apply --agent <agent> --json`. A current pin is a true no-op. An old pin is updated to the highest annotated immutable `vX.Y.Z` release with rollback evidence. Missing tags, an invalid resulting contract, or an overlapping dirty `.go` migration blocks work; never substitute mutable `main` or `GO_STACK_ALLOW_DEV=1`.
2. Read `.go/vision.json`, `.go/architecture-principles.json`, `.go/hierarchy.json`, and the selected task JSON.
3. Run `python3 cli/go.py validate .` and `python3 cli/go.py status . --json`.
4. Inspect routing with `python3 cli/go.py router . --command go --intent "$PROMPT_TEXT" --json`, announce `Route: <selected_route>`, then invoke the internal CLI primitive without asking for another user command.
5. Create or repair a concrete `.go` task before changing code when the requested work is not already represented.
6. Execute one task at a time inside its `scope.modify`, using its acceptance and verification commands.
7. Treat first green as provisional: run the relevant tests plus a critic/recheck pass, repair blocking findings, then finish the task with evidence.
8. `Go plan` stops before implementation. Otherwise continue until no open work remains, a repository gate blocks progress, or a declared budget is exhausted. Never report `done` while open tasks remain.

For local development, run:

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest tests/test_smoke.py -q
make check
python3 cli/go.py template-check ../go-project-template --json
```

Preserve unrelated user changes. Do not push unless the user explicitly requests it or the selected run has `--ship-policy push --allow-push`.

## GitHub Actions boundary

GitHub Actions are off limits. Do not create, edit, enable, trigger, dispatch, inspect, wait for, or use GitHub Actions workflows/checks as verification evidence. Existing files under `.github/workflows/` are not authorization to interact with GitHub Actions. Use local checks, a local Linux container, or another explicitly approved verification route instead.

<!-- go-workflow:agents-gateway:v1:start -->
## Repository-local Go workflow gateway

This repository uses `.go/` as its project workflow source of truth. Keep repository-specific instructions outside this managed block; they remain binding.

When the user invokes `Go`, `Go plan`, `Go <task-id>`, `Go loop`, or asks to continue autonomously:

1. Run the immutable stack-freshness preflight required by the pinned `.go/project.json` before routing or product edits.
2. Read `.go/vision.json`, `.go/architecture-principles.json`, `.go/hierarchy.json`, and the selected task.
3. Run the repository-local `validate`, `status`, and `router` commands, then announce `Route: <selected_route>`.
4. Create or repair a concrete `.go` task before changing product files. Execute one task at a time and stay within its `scope.modify` boundary.
5. Record content-bound verification evidence, run the required critic/recheck, repair blocking findings, and satisfy finish plus any required release evidence.
6. Continue through remaining in-scope work until the goal is met, a declared budget is exhausted, or a real repository gate blocks progress. Never call an empty queue `done` without auditing the original outcomes.
7. “Go tot alle taken klaar” freezes all unfinished in-scope task IDs, including blocked tasks, into the existing campaign controller. Show scope and dependency order; do not invent a five-task or two-hour ceiling. Explicit user budgets still apply.
8. Finish each task through claim, implementation, verification, independent review, commit, authorized push, configured deployment/readback and synchronized closure before announcing done and starting another. New authorized taskwise runs default to push; user/repository restrictions and frozen older-run authority prevail. Deployment without a requirement is not applicable; required unconfigured deployment is blocked.
9. Use the versioned progress outbox and an independently capable transport for task start, phase, repair, amendment, done and final messages. Preflight transport before unattended execution. A separate watcher offers a heartbeat every 300 seconds; no connected transport means no promised chat heartbeat. Retain undelivered events and stop before the next task until delivery recovers.
10. Resume from current canonical task, workspace, proof and publication records. Keep necessary repair tasks linked to original outcomes, show amended future tasks, and continue independent work around concrete blockers. Never count status/log churn as proven progress or expand an old run’s authority implicitly.

Do not redirect repository workflow state to a hidden central queue or retired vault. Nested `AGENTS.md` files may add directory-specific obligations, but they do not replace the root gateway or `.go` source of truth.
<!-- go-workflow:agents-gateway:v1:end -->
