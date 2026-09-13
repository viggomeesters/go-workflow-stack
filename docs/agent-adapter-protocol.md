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
