# Architecture

## Core split

```text
go-workflow-stack      reusable schemas + CLI + fixtures
        │
        └── validates / initializes
              │
project repository ─── .go/ JSON + JSONL state
```

The executable compatibility surface remains `cli/go.py`. Focused importable modules under `go_workflow/` own version constants, contract migrations, and the versioned agent-adapter protocol so those rules can evolve and be tested without expanding the CLI monolith.

## Source of truth

- Stack repo owns the reusable command surface and schema examples.
- Project repos own `.go/` execution state.
- External vaults may index or reflect state, but they are not required for clone-local continuation.

## Design principles

- Repo-local state over central task database.
- JSON for current state.
- JSONL for append-only evidence and lifecycle events.
- Scoped dirty policy instead of clean-repo absolutism.
- Synthetic public fixtures only.
