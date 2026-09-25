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

## Live terminal beside the chat

Select the terminal once in a consumer repository:

```sh
./go progress terminal . --enable
```

New Go campaigns then open a live Terminal window on macOS, or an available
`x-terminal-emulator` on a graphical Linux desktop. The project stores only the
portable terminal preference. Campaign intake resolves its repository/campaign
identity into the frozen transport command. Saved campaigns keep their transport
until explicitly revised; selecting the terminal never changes their permissions.
An existing different project transport is preserved and requires explicit
reconciliation before replacement.

Reopen a campaign in an attached terminal with:

```sh
./go progress watch . --campaign <campaign-id>
```

This replays the same stored messages and follows new ones. It does not restart
tasks. The viewer reads the existing outbox, prints and flushes each event to an
actual TTY, then records a content-bound rendering receipt. Only then can the
transport acknowledge delivery. This proves terminal output, not human reading
or chat delivery. The independent watchdog continues to supply 300-second
heartbeats while the model/controller waits.

Closing the window stops the viewer, not the running build. Unrendered messages
remain pending and prevent the next task from starting. Resume opens the window
again; lost acknowledgments retain the same event IDs. A crash between printing
and recording a receipt can replay a line; reopening explicitly replays history.
Only one receipt-writing viewer per campaign is allowed. A live unresponsive
viewer, foreign-host owner, refused launch or missing desktop produces a concrete
error rather than a delivery claim. A manually attached TTY also works without a
desktop launcher.

Task text is escaped before terminal rendering, so embedded ANSI/OSC controls
cannot affect the terminal. Machine-local viewer liveness and rendering metadata
(`*.terminal.json`) should be ignored by Git; canonical events and delivery
acknowledgments remain under `.go/runs/progress/<campaign-id>.json`.
