# Repository-local Go workflow

This repository develops the new JSON-first `.go/` workflow stack. Inside this repository, `.go/` is the source of truth; do not require `.go-workflow/config.yaml` and do not route through the legacy Life OS pipeline.

`Go` is the single public repository-work command. `Go plan`, `Go T123`, and
`Go loop 2h` are modifiers of that command; `auto`, `go-loop`, task creation,
claim, and finish are internal CLI primitives, not a menu the user must choose.

When the user says `Go`, uses a Go modifier, says `Next`, or asks to continue autonomously:

1. Read `.go/vision.json`, `.go/architecture-principles.json`, `.go/hierarchy.json`, and the selected task JSON.
2. Run `python3 cli/go.py validate .` and `python3 cli/go.py status . --json`.
3. Inspect routing with `python3 cli/go.py router . --command go --intent "$PROMPT_TEXT" --json`, announce `Route: <selected_route>`, then invoke the internal CLI primitive without asking for another user command.
4. Create or repair a concrete `.go` task before changing code when the requested work is not already represented.
5. Execute one task at a time inside its `scope.modify`, using its acceptance and verification commands.
6. Treat first green as provisional: run the relevant tests plus a critic/recheck pass, repair blocking findings, then finish the task with evidence.
7. `Go plan` stops before implementation. Otherwise continue until no open work remains, a repository gate blocks progress, or a declared budget is exhausted. Never report `done` while open tasks remain.

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
