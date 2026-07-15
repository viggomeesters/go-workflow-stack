# State safety and concurrency

Repository state uses one Unix/Linux/WSL process-safety layer in `go_workflow.state_io`:

- JSON objects are written to a same-directory temporary file, flushed with `fsync`, and atomically replaced.
- Task queue transitions update the source record and atomically move it between `open`, `active`, `blocked`, and `done` while holding a task-specific process lock.
- JSONL appends hold a stream-specific `flock`, append one complete line, and flush it before release.
- Git repositories keep lock metadata under `.git/go-workflow-locks`, so runtime locks never dirty project state. Non-Git fixtures fall back to `.go/locks`.

Lock files contain PID and status metadata for diagnosis, but ownership is decided by the operating-system lock. A contender waits for the kernel lock and never steals it merely because the metadata looks old. If a process dies, the kernel releases ownership; the next holder records `recovered_stale: true` when it sees unreleased metadata from the dead PID.

This design targets macOS, Linux, and WSL. It deliberately relies on `fcntl.flock`; native Windows without WSL is outside the supported execution contract.
