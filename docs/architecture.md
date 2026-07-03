# Architecture

## Core split

```text
go-workflow-stack      reusable schemas + CLI + fixtures
        │
        └── validates / initializes
              │
project repository ─── .go/ JSON + JSONL state
```

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
