# Task workspaces

Opt-in `execution_contract.workspace` uses `mode: task_worktree` and
`control_state: repo_local_single_writer`. Supply an explicit base branch;
creation additionally requires its exact commit, a new branch, absolute path,
active task owner and run identity. There is no main/master inference.

From the primary control checkout, using its pinned Go launcher:

```sh
./go workspace create . --task-id T038 --owner codex --run-id run-1 \
  --path /absolute/path/to/T038 --branch task/T038 \
  --base-branch release-base --base-commit <40-character-commit> --json
./go workspace status . --task-id T038 --owner codex --run-id run-1 --json
./go workspace stage . --task-id T038 --owner codex --run-id run-1 --json
```

These are internal controller primitives. Fresh-worker startup and active-task
resume orchestration follow in abc-03/04. Existing unregistered worktrees remain
compatible; creating a task contract does not silently convert one. Hook dispatch
for an opted-in task refuses to run until its registered workspace exists. A
registered worker must match its current canonical task snapshot and claim.
Model and phase changes reuse its workspace and execution lease.

Managed verification that is not part of controller-owned publication runs in
an unregistered disposable checkout. The controller clones the worker's exact
HEAD, overlays every changed or untracked candidate path, and compares the
resulting tracked-content and changed-file hashes before starting a check. Each
check records those source hashes together with the managed candidate digest.
This lets repository-wide checks create and mutate their own temporary fixture
repositories without inheriting the registered worker boundary. The registered
workspace remains read-only during verification, and its normal command guard
still rejects direct workflow-state mutation.

## One control state

The primary checkout's `.go/workspaces/<task>.json` owns the workspace registry.
A marker in each worktree's Git metadata binds repository UUID, task, generation,
path and control root. Git discovery supports `.git` files and `git-common-dir`.
Repository state locks use the common Git directory; Git prune locks are not
execution or integration locks.

CLI reads in registered worktrees resolve `.go` to the canonical control root.
The copied tracked `.go` data is inert. Workflow mutations run from the control
checkout; the only worker mutation allowed is an outcome for its active task,
with its matching owner. Direct copied task/run/evidence writes are scope
violations. A Git worktree is not a security sandbox: filesystem permissions,
service ports, databases and hostile subprocess isolation need separate policy.

## Ownership and recovery

Create is serialized and repeatable for the exact same owner/run/path/base.
Unknown existing paths, branches and mismatched identities are preserved and
rejected. An interrupted `creating` record recovers only an exact, clean Git
worktree at its recorded base. Orphan branches or unexpected content require
inspection; they are never automatically deleted. Missing markers on known
worktrees fail closed instead of enabling a second task queue.

`workspace rebind` takes the old `--owner` / `--run-id` and explicit `--new-owner`
/ `--new-run-id`. It follows an already transferred canonical active claim,
under the execution and task locks. It does not transfer claims itself. It
retains ownership history, Git identity, content and base. Live execution holds
the execution lock throughout the subprocess; another writer cannot start.
Registry paths are machine-specific. A clone keeps historical records but cannot
reuse a different machine's workspace without an explicit future migration.

## Scope and integration

Scope checks include the complete net diff from the recorded base, the index,
working files and untracked paths. Renames check both sides. Staging is limited
to the validated paths with literal Git pathspecs. Canonical user dirt is never
copied, staged or reset. Runtime task/run/evidence data must use the control root.

The Python `integration_slot(control, task_id, owner, run_id)` context manager
holds the repository integration lock and task execution lock across the caller's
operation. It yields exact base and candidate revisions after identity, clean
state, scope and unchanged-base checks. The CLI `workspace integration-check`
is only a preview: its lock is released when the command returns. A publisher
must hold the Python context across integration; this module does not publish.
An advanced base is retained as a reconciliation blocker. The controller can run
`workspace reconcile` for one clean, idle, owned workspace after the control
checkout has advanced linearly on the registered base branch. Reconciliation
merges the new base without rewriting task history, records both exact histories,
updates the managed binding, and invalidates verification and critic evidence so
the final candidate is proved again. It never resets the control checkout or
touches unregistered branches/worktrees. A merge conflict is aborted to the exact
prior worker head and reports conflict paths; out-of-scope conflicts are explicitly
identified. Existing publication intent blocks reconciliation and must be resumed
through publisher readback instead.

After actual integration, `workspace record-integration --integrated-commit SHA`
(with the same repo/task/owner/run flags) verifies that the complete workspace
history is reachable through that commit on the configured base branch. It stores
that proof without merging or committing. Cherry-pick/squash integration cannot
satisfy this ancestry contract; use history-preserving integration.

## Cleanup

`workspace cleanup` requires a completed, approved canonical task, recorded
integration, unchanged workspace HEAD and a release receipt when policy requires
one. It checks receipt task/project binding and that its commit preserves the
workspace on the base branch. An explicit no-release policy needs its reason.
Receipt producers remain responsible for the release verification contract;
stronger publisher/final-candidate verification follows in abc-05/06.

Tracked, untracked **and ignored** dirty files block cleanup. Git removes only
the verified worktree without force; the delivered branch remains. A Git lock or
removal error records `cleanup_failed` with an inspect-and-retry action. It never
republishes or erases the receipt. Missing paths still registered in Git require
inspection; an already removed, unregistered workspace can complete interrupted
cleanup from the retained integration and delivery proof. A cleaned run is not
recreated.
