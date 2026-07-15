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

The stable implementation owners are importable from `go_workflow.adapters` and `go_workflow.adapter_protocol`. Pure command routing lives in `go_workflow.routing`, while read-only task path and queue queries live in `go_workflow.task_state`; `cli/go.py` remains the compatible command facade.

Validate a captured result without executing an agent:

```bash
python3 cli/go.py adapter validate-result result.json --phase repair --json
```

Protocol schemas live in `schemas/agent-adapter-request.schema.json` and `schemas/agent-adapter-result.schema.json`.
