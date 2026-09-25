# Authorized taskwise delivery implementation

Source: current user instruction “PLEASE IMPLEMENT THIS PLAN: Go-workflow-stack:
autonoom opleveren met zichtbare voortgang per taak”. The five canonical task
records taskwise-t01 through taskwise-t05 are authoritative for status. This brief
preserves scope; it is not a separate queue.

Work only in go-workflow-stack, instructions/templates and agent-neutral progress
contracts. Preserve divergent local stack commits and uv.lock; use the isolated
codex/taskwise-delivery worktree from published origin. Do not modify or restart
the old Hermes runner. Book Forge models/generation are outside this assignment.
No GitHub Actions. Commit and push are authorized; deploy only to configured
existing destinations. Preserve older runs' original authorization.

1. **taskwise-t01:** Freeze original unfinished tasks/outcomes, including blocked
   tasks. Until-scope execution has no implicit total task/time ceiling. Preserve
   explicit budgets, pause/cancel/provider boundaries and legacy contracts.
   Administrative activity is not evidence of progress. Admit only necessary
   repairs; other ideas stay in backlog. Empty runnable queue is not success.
2. **taskwise-t02:** Complete each task through verification, critic, commit,
   push, configured deployment/readback and synchronized closure before the next.
   No-release tasks still follow explicit shipping policy. Missing required
   deployment is a blocker, not invented configuration. Separate repair from
   frozen versioned candidate; invalidate dependent proof when content changes.
   Recover every external effect by readback. Fresh checkouts must see closure.
   Jointly deliverable tasks retain IDs/outcomes and finish only after shared
   proof/publication. Preserve unrelated staged and unstaged work.
3. **taskwise-t03:** Extend existing failure/selection logic. Two substantively
   equal failures require a changed diagnosis/method with relevant evidence.
   After two strategies, reassess whether a safe supported route remains; this
   does not cap the full run. Necessary outside-scope repairs become linked
   subtasks; no recursive infrastructure chains. Block unsafe routes and continue
   independent work. Amend only future unfinished tasks with reason and validated
   dependencies; preserve completed acceptance and original outcomes.
4. **taskwise-t04:** Reconstruct resume context from current task, repository,
   workspace, proof, external effects and next action. Old chat is background.
   Check actual owner/process liveness; timeout does not release a writer. Source
   drift invalidates only dependent proof. Preserve completed checkpoints and
   begin resumed chat with what is complete and where work resumes.
5. **taskwise-t05:** Versioned adapter progress events, stable IDs/order, durable
   outbox/receipts. Separate watcher offers heartbeat every 300 seconds even during
   model/tool silence; reports phase, elapsed time and confirmed activity or
   unknown. Missing signals trigger diagnosis, not fabricated recovery/completion.
   Real chat delivery requires independently capable transport; preflight it and
   fail honestly if absent. Done acknowledgement gates next task; retry queued
   events idempotently. Provide local reference adapter/conformance tests.

Per-task execution is serial. Independent reviewers/research agents may assist
without overlapping write scopes. Announce existing task IDs, heartbeat/activity,
blockers/repairs/amendments and done results. Final summary gives actual counts
and publication states, never a time percentage inferred from task count.

Final verification: ten disposable tasks with local Git remote, test deployment
and recording chat transport; independent deliveries, repair insertion,
nonrepeating failure strategies, independent continuation, acyclic amendments,
candidate drift, interruption at each publication step, fresh-session recovery,
model silence plus heartbeat and transport outage recovery. Use controllable
clock tests AND a real duration test over two 300-second intervals. Confirm old
single-task and saved campaign compatibility.

Rollout: one immutable release after relevant full regressions, integrated trial
and independent review. Then update one explicitly designated consumer and prove
bounded actual execution INCLUDING live chat delivery. Preserve other pins and
rollback. Real consumer/channel selection was requested asynchronously; no
independent message injection into the active Codex chat is currently available
through the exposed tools. Do not claim full plan completion without live proof.
