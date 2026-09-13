# State safety and concurrency

Repository state uses one Unix/Linux/WSL process-safety layer in `go_workflow.state_io`:

- JSON objects are written to a same-directory temporary file, flushed with `fsync`, and atomically replaced.
- Task queue transitions update the source record and atomically move it between `open`, `active`, `blocked`, and `done` while holding a task-specific process lock.
- JSONL appends hold a stream-specific `flock`, append one complete line, and flush it before release.
- Git repositories keep lock metadata under `.git/go-workflow-locks`, so runtime locks never dirty project state. Non-Git fixtures fall back to `.go/locks`.

Lock files contain PID and status metadata for diagnosis, but ownership is decided by the operating-system lock. A contender waits for the kernel lock and never steals it merely because the metadata looks old. If a process dies, the kernel releases ownership; the next holder records `recovered_stale: true` when it sees unreleased metadata from the dead PID.

This design targets macOS, Linux, and WSL. It deliberately relies on `fcntl.flock`; native Windows without WSL is outside the supported execution contract.

Managed task runs additionally keep `.go/runs/<task>/run-state.json`. One
`managed-run-<task>` lock covers the controller invocation; short state locks
serialize worker registration and checkpoint writes. Before any command runs,
its child bootstrap records the new process group against the controller host,
PID, run and unique phase nonce. A successor verifies that both the previous
controller and every non-zombie member of that group have stopped. Expired
metadata, a reused PID and an unknown remote host cannot authorize takeover.
Detached background workers are outside this supported process contract.
Standalone workspace execution, stage, integration, rebind and cleanup apply
the same liveness check, even after a controller crash releases its kernel lock.

Setup intent is durable before claim/worktree creation. Confirmed phases and
individual verification commands survive renewed budgets. Interrupted phases
retain their evidence and Git state; unknown results are not upgraded to success.
Task/model changes require explicit migration. Changed workspace bytes, index,
HEAD/base or canonical instructions invalidate affected check/critic proof while
preserving files. A critic or verification command that changes tracked/task
content cannot supply passing proof for that content.

`RunSession.effect_intent` saves a stable effect key, target and expected result
before an external effect. Existing intents return `fresh=False`; callers must
use `reconcile_effect` with actual readback before deciding what to do. Unknown
or absent external results remain pending. This interface does not itself push,
publish or deploy. A completed task in cleanup recovery only retries validated
workspace cleanup, retaining its release receipt.

`managed relocate` repairs one explicitly moved primary/worker pair on the same
host. It requires absent old paths, the existing Git repository UUID and marker,
stopped processes and matching task/owner/run. A durable path mapping makes
interrupted Git repair retryable. Historical snapshots are never rewritten;
current verification is regenerated. Foreign clones, another host, publication
in progress and multiple non-cleaned workspace registrations require explicit
reconciliation. Ordinary unregistered worktrees retain the legacy behavior.
