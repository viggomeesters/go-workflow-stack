# Taskwise progress delivery

New public Go executions use a taskwise campaign for the selected existing or
newly adopted tasks. Bare Go selects one task; explicit counts and until-scope
requests define their own scope. Existing campaign and managed-run resumptions
retain their frozen authority. These new campaigns freeze `execution.progress` alongside existing shipping
and scope authority. Configure `progress_transport` in `.go/project.json` before
intake. The command is an argv array (never shell text), with a timeout of at most
60 seconds:

```json
{"schema":"go-workflow.progress-transport.v1","command":["/absolute/path/chat-adapter"],"timeout_seconds":10}
```

The command receives one JSON request on stdin and returns one JSON object on
stdout. `operation=preflight` must return `capabilities.independent_messages=true`
and `capabilities.idempotent_event_ids=true`. `operation=deliver` includes the
versioned `event`; return its exact `id` as `event_id` and a nonempty `receipt`
only after durable delivery. A transport must deduplicate on event ID, including
when delivery succeeds but acknowledgement is lost. An acknowledgement of a local
queue proves that queue's acceptance, not human reading or a downstream chat.

The outbox under `.go/runs/progress/` records stable sequential IDs, semantic
retry keys, payloads, rendered messages and acknowledgments. It is delivery
metadata; canonical tasks, campaigns and run records still own execution state.
Concurrent emitters use a separate lock. Only the earliest pending event can be
acknowledged. Done events require verified task delivery and closure; every
pending event must drain before another task dispatch. A transport fault preserves
the task's actual completion and publication evidence and stops the campaign at
this delivery boundary. Resume retries the same event identity.

The controller emits task, phase, recovery, amendment, resume and final events.
An independently supervised process observes canonical run records and offers a
heartbeat every 300 seconds, even while the controller waits on a model or tool.
A recorded operation start is not proof that the process is still making progress.
Unknown activity is explicit. Missing signals produce a diagnostic observation;
the watcher never repairs, terminates workers or declares completion. A long test
is not automatically a stuck task. Startup requires the child readiness receipt;
next dispatch checks watcher liveness. Persistent delivery failure remains a
concrete limitation, not an excuse to claim heartbeat delivery.

Local reference and conformance tests:

```sh
python3 -m go_workflow.progress_transport record --path /absolute/path/messages.json
python3 -m pytest tests/test_progress_events.py tests/test_progress_transport.py tests/test_progress_watchdog.py tests/test_campaign_progress.py -q
```

The recording reference explicitly advertises `user_chat=false`. It is useful for
integration tests but cannot satisfy a real connected chat rollout. A production
adapter must implement the same handshake, ordered stable-ID acknowledgement,
lost-ack replay, independent delivery during model silence and outage recovery.

Existing contracts without progress remain readable and retain their authority.
For an existing taskwise campaign, explicitly opt in using `campaign progress
<repo> --campaign <path> --request <json> --agent <owner>`. The request contains
`change_id`, `transport`, `reason`, and source-bound `evidence` (path and SHA-256).
The revision journal preserves task outcomes, permissions, consumed budgets and
valid proofs. It neither grants push/deployment nor converts a legacy bounded run
into an unlimited one. Recover an interrupted revision using the identical request.


## Preserved publication candidates

When bounded recovery fails before any publication effect, the controller can
suspend the owned prepared candidate and release its unused publication channel.
The workspace and proofs remain intact; interruption recovery finishes that exact
suspension before isolating the task. An already suspended blocked task may only
finish its recorded reservation cleanup. It cannot prepare or publish the old
candidate. Resuming that product work requires explicit fresh-candidate
reconciliation; the runtime reports that limitation instead of guessing a version
or discarding the old candidate.
