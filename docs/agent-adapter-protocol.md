# Agent adapter protocol

Codex, Hermes, and custom commands share one versioned JSON boundary. Every build, critic, or repair process receives the complete request in `GO_ADAPTER_REQUEST_JSON`.

The request uses `go-workflow.agent-adapter-request.v1` and contains the phase, repository, task, attempt, strategy, execution context, and required result schema. `GO_TASK_JSON` and `GO_CONTEXT_JSON` remain available for compatibility.

Adapters should print one compact JSON result line:

```json
{
  "schema": "go-workflow.agent-adapter-result.v1",
  "phase": "repair",
  "status": "success",
  "summary": "Repaired the failing parser and ran its focused test",
  "evidence": ["pytest tests/test_parser.py -q"]
}
```

The conductor validates a protocol result, attaches process metadata, and fails closed on an invalid phase/status/schema. Built-in Codex and Hermes adapters must emit this result natively for every build, critic, and repair phase; ordinary prose from a built-in adapter is a protocol failure. A custom shell adapter that prints ordinary text is still wrapped according to its exit code for backward compatibility. Any JSON object that looks like protocol output fails closed when malformed instead of falling back to that compatibility path.

Hermes prompt input is capability-detected from the installed executable's `--help` output. Current Hermes releases use `-z PROMPT`; legacy `-p PROMPT` remains supported when explicitly advertised. `agent-check` and `doctor` report the detected `prompt_flag` and mark a Hermes executable incompatible before a build/critic/repair loop starts when neither interface exists.

```bash
python3 cli/go.py agent-check --agent hermes --json
python3 cli/go.py doctor . --platform wsl --agent hermes --json
```

The stable implementation owners are importable from `go_workflow.adapters` and `go_workflow.adapter_protocol`. Pure command routing lives in `go_workflow.routing`, while read-only task path and queue queries live in `go_workflow.task_state`; `cli/go.py` remains the compatible command facade.

Validate a captured result without executing an agent:

```bash
python3 cli/go.py adapter validate-result result.json --phase repair --json
```

Protocol schemas live in `schemas/agent-adapter-request.schema.json` and `schemas/agent-adapter-result.schema.json`.

## Opt-in model control (v0.3.16)

Tasks with an `execution_contract` use the stored `model.id` and `model.effort`
for native Codex build and repair. `critic_model` overrides review only. The
conductor passes `--model`, `--config model_reasoning_effort=...`, and `--json` to
the executable. These explicit arguments take precedence over model defaults.

Before a worker starts, the installed Codex app-server's `model/list` catalog is
queried from the execution repository. All three phase profiles must resolve to
an advertised model and supported reasoning effort. Missing executables,
malformed/failed/timed-out catalog responses, unknown models and unsupported
efforts stop execution. Catalog availability does not guarantee service uptime,
quota or successful inference; worker errors remain failures. No model fallback
or automatic escalation is introduced.

The first successful preflight stores `.go/runs/<task-id>/model-selection.json`
under a process lock. Later phases check that snapshot and the persisted task.
Changing project defaults does not change an already explicit task profile;
changing the task's frozen selection requires a deliberate future run migration.
Active-task resumption and controlled mid-task model changes belong to `abc-04`.

Controlled model execution currently supports native Codex phases. `agent-check`
reports the adapter's model-control capability. Hermes and opaque custom shell
adapters explicitly report unsupported model control. A configured custom build,
critic or repair is rejected before an opted-in agent task's first build. Legacy
tasks without `execution_contract`, including mechanical tasks, retain their
existing adapter behavior. This release does not silently replace a custom
adapter with Codex.

Requests and normalized results include conductor-owned `model_selection`:
`requested` identifies the enforced arguments, while `effective: null` and
`confirmation: unconfirmed` state that independent effective-model confirmation
is unavailable. Agent prose is not confirmation. The conductor overwrites worker
claims in these reserved attribution fields and records phase-start/completion
events in `.go/runs/events.jsonl`.

For controlled Codex execution, agent text is extracted from native JSON
`item.completed` events. Token counts come only from native `turn.completed`
usage records and remain separate per turn. Process elapsed time is measured;
missing usage remains null. Provider invoice cost is null: no price estimates or
subscription usage are reported as a bill. Schema/CLI result validation checks
the supported attribution shape, without authenticating arbitrary imported logs.

The subprocess tests use a protocol fixture to verify arguments, refusal before
worker writes, frozen-profile changes and native-event attribution. The local
catalog readback is real; these tests do not claim a live model generation.
End-to-end worker topology/model-switch proof remains `abc-09`/`abc-10`.

Protocol reference: [Codex App Server — model/list](https://learn.chatgpt.com/docs/app-server#list-models-modellist).

## Durable context for managed native workers (v0.3.18)

A native protocol phase in a registered task worktree receives a durable context
reference. Legacy/unregistered adapters keep the existing inline environment;
a legacy phase never inherits a different worker's snapshot variables. Custom
model control remains unsupported; this is not a new custom-adapter bypass.

Before launch, the controller creates an immutable directory under canonical
`.go/runs/<task>/<run>/<phase>-<attempt>-<uuid>/`. The UUID prevents repeated
attempt numbers from overwriting earlier context, raw process output, or managed
attempt/critic artifacts. Partial captures after a crash remain for inspection;
a new dispatch creates a new identity instead of rewriting them.

`context.json` contains current project/vision/principles/hierarchy, task scope,
requirements and remaining outcomes, applicable architecture, task and execution
contract hashes, workspace/owner/run identity, base/current Git revisions, index
fingerprint, actual tracked-content fingerprint, changed/untracked file hashes,
phase contract, current checks/critic/build feedback, and confirmed task release
receipt when one exists. Code and raw evidence remain authoritative. Exact diffs
and changed/new file bytes are retained beside the snapshot; the current
workspace and Git history remain the code source, not a prose summary.

The request's optional `context_ref` is `{path, sha256, snapshot_id}`. In this
mode its `task` contains identity and its `context` contains the reference;
`GO_TASK_JSON` and `GO_CONTEXT_JSON` are also small references. The complete
context is stored once on disk rather than copied repeatedly into environment
variables. `GO_CONTEXT_PATH`, `GO_CONTEXT_SHA256` and
`GO_CONTEXT_VERIFY_COMMAND` expose the explicit read/verify boundary.

The parent and the child bootstrap both verify the snapshot before the worker
command executes. The bootstrap uses the current Python runtime's CLI module,
including installed distributions. It checks hashes, exact run/phase/attempt
path, canonical task/control sources, workspace ownership, HEAD/index/current
tracked bytes, untracked files, selected references, raw blobs and feedback
file references. Git index flags cannot conceal content drift; staging,
integration and cleanup reject flags that can hide modified files. An unchanged
HEAD alone is insufficient. Checks run under the workspace execution lease.

A worker can repeat verification with the command in
`GO_CONTEXT_VERIFY_COMMAND`, or call:

```sh
./go context verify /absolute/workspace --task-id T038 \
  --snapshot /absolute/control/.go/runs/T038/run-1/build-01-UUID/context.json \
  --sha256 SHA256
```

Changed code or structured state requires a fresh snapshot. Completed outcome
updates are reloaded from canonical task state for the next phase; changing
scope, model, claim or other protected task inputs rejects the old dispatch.
Normal product edits intentionally make the previous snapshot historical.

The controller passes current failed checks and critic findings explicitly to
repair before starting it. The result includes `process_result_ref` with the
hash of full raw process output. Phase feedback may carry `evidence_refs`,
`context_ref` and `process_result_ref`; those typed references must resolve to
canonical run/evidence files and their hashes are rechecked before launch.
Arbitrary agent text or arbitrary `path` fields are not promoted into authority.

Optional task `context_files` and `skill_files` select repo-relative files, as
lists or maps from `build`/`critic`/`repair` to lists. Only the current phase's
selection is loaded; global skill folders and the entire read scope are not
implicitly copied. The required root `AGENTS.md` gateway is included; validation
fails before adapter execution when it is missing or stale. Optional
`notepad_path` is supplementary explanation and cannot override structured
state or proof. Escaping/missing references are rejected. Individual selected
files, changed-file captures and snapshot JSON have an explicit 16 MiB limit;
reduce selected inputs before retrying instead of silently truncating evidence.
Unchanged tracked files are fingerprinted by streaming their bytes.

This release supplies durable dispatch context and fixtures proving a fresh
process can consume it. Active-task resume, moved-checkout recovery and complete
phase orchestration remain in abc-04; live multi-model worker proof remains in
abc-09/10. The legacy attempt projection is kept for old adapters; managed
attempt references point to distinct canonical artifact paths.

## Independent progress channel

Progress delivery is separate from the blocking build/critic/repair request. The
versioned `go-workflow.progress-event.v1` and `go-workflow.progress-transport.v1`
contracts support durable task messages and independently supervised 300-second
heartbeats. See [progress delivery](progress-delivery.md) for the handshake,
receipt semantics, local reference and conformance suite. A model's ordinary final
response does not provide independent delivery capability; preflight that capability
before an unattended campaign. Existing worker result schemas remain unchanged.
