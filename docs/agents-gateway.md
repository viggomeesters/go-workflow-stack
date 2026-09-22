# Root AGENTS.md gateway

Every repository that owns a `.go/` contract must also own an exact-case
`AGENTS.md` file in the repository root. `.go/` remains the source of truth for
project state; root `AGENTS.md` is the discovery and execution gateway that tells
a newly arrived agent how to enter that state safely.

Validation fails closed when the file is missing, uses another casing such as
`agents.md`, is a symlink, or does not contain the current managed gateway block.
The failure always names the bounded repair command:

```bash
go-workflow agents sync . --apply
```

Run the command without `--apply` for a read-only plan. The managed block is
delimited by versioned HTML comments. Synchronization creates the file, appends
the block to an existing custom file, or replaces only the bytes between one
matched marker pair. Text outside the pair is preserved. Multiple or unmatched
markers are ambiguous and block automatic writes until a human restores one
clear pair.

The block requires an agent to:

- use `.go/` as repository-local workflow state;
- run the immutable stack-freshness preflight;
- read vision, architecture principles, hierarchy, and the selected task;
- run validate/status/router and announce the selected route;
- execute one scoped task at a time;
- bind verification and critic/repair evidence to completion and release;
- audit original outcomes before declaring the goal done.

Repository-specific rules belong outside the managed block and remain binding.
Nested `AGENTS.md` files may add directory-local constraints, but may not replace
the root gateway or redirect workflow state to another queue or vault. The
gateway deliberately carries no unrelated organization policy.

`init`, `adopt`, and `spike` install the gateway. Ordinary `migrate --apply` and
`stack update --apply` repair it transactionally; their dry-runs report the
planned `AGENTS.md` operation. A stack-update rollback restores the prior file
content and filename. This gives both fresh repositories and older adopted
repositories one supported path to the same contract.
