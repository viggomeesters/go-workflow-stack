"""Deterministic native worker double with one injected critic failure."""
import json
import os
from pathlib import Path
import sys


if sys.argv[1] == "app-server":
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        result = {} if request["method"] == "initialize" else {
            "data": [
                {"model": "gpt-5.6-terra", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
                {"model": "gpt-6-astra", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]},
            ],
            "nextCursor": None,
        }
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
else:
    request = json.loads(os.environ["GO_ADAPTER_REQUEST_JSON"])
    snapshot = json.loads(Path(request["context_ref"]["path"]).read_text())
    task = snapshot["context"]["task"]
    phase = os.environ["GO_HOOK"]
    capture = Path(os.environ["AUTONOMY_CAMPAIGN_CAPTURE"])
    previous = [json.loads(line) for line in capture.read_text().splitlines()] if capture.exists() else []
    first_alpha_critic = (
        phase == "critic"
        and task["id"] == "deliver-alpha"
        and not any(item["phase"] == "critic" and item["task_id"] == task["id"] for item in previous)
    )
    if phase in {"build", "repair"}:
        artifact = Path("alpha.txt" if task["id"] == "deliver-alpha" else "beta.txt")
        artifact.write_text("alpha\n" if task["id"] == "deliver-alpha" else "beta\n")
    with capture.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "task_id": task["id"],
            "phase": phase,
            "cwd": str(Path.cwd()),
            "argv": sys.argv[1:],
            "feedback": snapshot.get("feedback"),
            "context_ref": request["context_ref"],
        }) + "\n")
    result = {
        "schema": "go-workflow.agent-adapter-result.v1",
        "phase": phase,
        "status": "blocked" if first_alpha_critic else "success",
        "summary": (
            "R1 forced behavioral finding: re-open the current artifact and verify exact bytes"
            if first_alpha_critic else "current campaign artifact inspected"
        ),
    }
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}}))
    print(json.dumps({"type": "turn.completed"}))
