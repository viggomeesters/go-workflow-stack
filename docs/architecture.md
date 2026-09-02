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
- Capacity before parallelism: default solo or lead-plus-one-worker, only parallelize disjoint modify scopes.
- Review, attribution, and evidence gates before any multi-agent UI expansion.

## Claim-to-ship provenance

Every new task claim records the exact Git commit and claim identity in both the
task and its matching append-only `task.claimed` event. An unborn repository uses
Git's canonical empty-tree SHA. `no_diff=true` is not a synonym for “the worktree
happens to be clean”: finish requires those claim records to agree, requires a
real commit base to be an ancestor of final `HEAD`, and compares both current
dirty paths and the committed `claim.base_commit -> HEAD` path delta, excluding
only workflow lifecycle state. Missing, unavailable, mismatched, or unrelated
equal-tree claim provenance fails closed.

Autonomous shipping keeps the evidence chain explicit:

```text
claim base -> finish evidence -> task delivery commit -> push target -> ls-remote readback
```

`push` is successful only when the independently read remote branch SHA equals
the exact local task-delivery commit. The done task's latest finish evidence is
then enriched with that commit and the exact `remote/branch` target resolved via
`branch.<name>.pushRemote`, `remote.pushDefault`, or `branch.<name>.remote`.
The workflow pushes the exact commit to the same-name branch, records the
observed remote SHA, and sets
`readback_verified=true`; the normal final run-state commit makes this
attestation durable. The attested task-delivery commit is intentionally the
parent of the attestation commit, avoiding an impossible self-referential Git
commit hash.
