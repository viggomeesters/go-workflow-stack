
# Go Workflow Stack

Reusable tooling for Viggo's repo-local agentic engineering contract.

Principle: **workflow tooling is reusable, project execution state is repo-local**.
A target project owns its `.go/` folder with JSON/JSONL state; this stack owns schemas, validators and CLI helpers.

## Current spike surface

```bash
python3 cli/go.py init ../some-project --force
python3 cli/go.py validate ../some-project
python3 cli/go.py next ../some-project
python3 cli/go.py claim task-schema-smoke --repo ../some-project --agent hermes
python3 cli/go.py finish task-schema-smoke --repo ../some-project --agent hermes --evidence "smoke passed"
python3 cli/go.py dirty-check ../some-project --owned '.go/**'
python3 cli/go.py readback ../some-project
```

## Boundaries

- JSON is canonical for current project state.
- JSONL is canonical for lifecycle/evidence/decision events.
- Markdown is generated/human-facing only.
- The Life OS vault can index/reflect repo state, but must not be required for clone-local continuation.
- No broad AW Lite migration lives here yet.
