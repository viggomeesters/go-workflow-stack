# Worker topology and model switching

Go supports fresh native Codex CLI workers at task/phase boundaries. The controller
owns `.go`, an execution lease and one task worktree. Build and repair use the
frozen task model/effort; critic may use the frozen `critic_model`. A model change
between those phases starts a fresh process in the **same** worktree with a new
verified canonical context snapshot. No new user-facing Codex task is needed.

Decision `abc-d02-worker-topology` is accepted. Persistent app-server session/turn
adapters and subagent adapters are deferred. They are not prerequisites for the
baseline or template release. Mid-turn switching is not supported. Editing a model
profile during an active frozen run is rejected; it requires explicit migration,
which is not supplied as an automatic profile-change operation.

| Topology | Context and recovery | Model attribution | Runtime dependency / support |
| --- | --- | --- | --- |
| Fresh CLI worker per phase | Canonical snapshot, raw feedback and owned workspace survive parent process exit; managed resume reloads checkpoints | Requested model/effort enforced as CLI arguments; effective identity remains unconfirmed | Supported Go native adapter, `codex exec --ephemeral`; local model catalog checked before launch |
| Persistent session with per-turn overrides | Exact thread ID and explicit per-phase overrides required; transcript cannot replace `.go`; ephemeral history must not be assumed resumable | Protocol accepts model/effort fields, but a future adapter must separately record request and runtime confirmation | Local app-server protocol capability confirmed; Go execution/recovery adapter deferred |
| Subagent with its own profile | Parent may disappear; child must get bounded canonical context and registered ownership; nested or parallel writers need additional coordination | Parent claims do not attest a child's model; child adapter must verify supported profile and report observed results | Go subagent adapter deferred; a host UI or exposed subagent tool is not an owned Go runtime |

## Evidence and its limits

`fixtures/abc-topologies/provenance.json` records **codex-cli 0.153.4**, local
inspection commands and SHA-256 hashes of the selected generated schemas and CLI
help. This is a snapshot of the installed official binary, not a requirement to
use that version forever or a live execution claim. The schemas were generated
without starting a model turn. They establish:

- `ThreadResumeParams`, `ThreadForkParams` and `TurnStartParams` require `threadId`.
  The protocol accepts a string; it does **not** prove that Go owns that session.
  A future adapter must bind the exact ID, never choose `--last` or guess a session.
- `TurnStartParams.model` and `.effort` override this and subsequent turns. A future
  adapter must explicitly select build/critic/repair settings every turn, including
  resetting a critic override before repair. This is a safe turn boundary, not a
  supported mid-turn mutation. The generated schema accepts arbitrary effort strings;
  actual model/effort availability requires separate model-catalog validation.
- Start/fork permit `ephemeral`; CLI help describes it as avoiding persisted
  session files. A returned session ID is not proof of durable recovery after
  parent/runtime loss. Use canonical Go context to reconstruct a fresh worker.

`tests/test_abc_topologies.py` exercises the actual native command, model catalog
handshake, context/bootstrap and workspace leases using an executable Codex double.
Separate parent processes run build, critic and repair. The test observes distinct
snapshot references, frozen phase arguments, one workspace generation, canonical
feedback and rejection of profile edits. A real competing process cannot obtain
the held writer lease. Versioned protocol schemas exercise explicit session IDs
and overrides without claiming a persistent adapter implementation.

The critic test verifies the native **read-only sandbox argument** and an unchanged
fixture worktree. The double does not implement Codex's OS sandbox. Provider model
execution, real sandbox enforcement and live task lifecycle proof are separately
tracked in `abc-10`; these fixture checks do not establish those claims.

## Contract for any future alternative adapter

Keep the canonical task/run/context and one workspace identity across every safe
boundary. Revalidate ownership, code and context before launch; preserve raw phase
results. A critic remains read-only unless a separate explicit transfer authorizes
writing. Use exclusive execution and publication ownership, and retain the existing
process-group/liveness checks before recovery or staging. A child tree or session
ID cannot bypass these checks or grant publication/deployment authority.

Before enabling an alternative, demonstrate parent loss, unavailable session,
unknown effect readback, frozen profile drift, failed critic/repair and a surviving
worker. Recovery must refuse a second writer and resume the exact pending stage.
Do not create a worktree per model or make every legacy skill mandatory. The useful
legacy ideas are bounded phase contracts, selective validators, durable handoff and
provenance; see `.go/plans/legacy-insights.json`, `task-workspaces.md` and
`agent-adapter-protocol.md` for the source trail and released control boundaries.
