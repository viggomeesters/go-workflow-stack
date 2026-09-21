import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run_go(*args, env=None):
    cwd = next((Path(str(value)) for value in args if (Path(str(value)) / ".go/project.json").is_file()), ROOT)
    return subprocess.run(
        [sys.executable, str(ROOT / "cli/go.py"), *map(str, args)],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )


def seed(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "fixtures/minimal", repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "seed"],
        cwd=repo,
        check=True,
    )
    return repo


def adapter(tmp_path):
    script = tmp_path / "assess.py"
    script.write_text(
        """import json, os
from pathlib import Path
request = json.loads(os.environ['GO_ADAPTER_REQUEST_JSON'])
capture = Path(os.environ['INTAKE_CAPTURE'])
with capture.open('a') as stream:
    stream.write(json.dumps(request) + '\\n')
assessment = {
    'schema': 'go-workflow.intake-assessment.v1',
    'request_sha256': request['task']['intake_request']['sha256'],
    'summary': 'One bounded documentation task.',
    'disposition': 'ready',
    'actions': [{
        'action': 'create', 'task_id': 'document-retries', 'summary': 'Document retry behavior',
        'work_type': 'implementation', 'root_cause': 'not_applicable', 'substantial': True,
        'outcome_ids': ['O1'], 'scope': {'read': ['docs/**'], 'modify': ['docs/retries.md']},
        'behavior': {'before': ['Retry behavior is implicit'], 'after': ['Retry behavior is documented'],
                     'states': ['ready', 'retrying', 'stopped'], 'edges': ['failure -> retrying']},
        'non_goals': ['Changing retry runtime'], 'dependencies': [],
        'delegated_choices': ['Choose headings within docs style'], 'question_ids': [],
        'acceptance': ['Retry states and stop behavior are documented'],
        'verification': ['test -f docs/retries.md'],
    }],
    'questions': [], 'relevant_decisions': [],
}
print(json.dumps({'schema': 'go-workflow.agent-adapter-result.v1', 'phase': 'critic',
                  'status': 'success', 'summary': 'Grounded intake assessment complete.',
                  'intake_assessment': assessment}))
"""
    )
    return script


def test_planning_intake_uses_adapter_once_and_reapply_is_idempotent(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    capture = tmp_path / "capture.jsonl"
    env = {**os.environ, "INTAKE_CAPTURE": str(capture)}
    command = f"{sys.executable} {script}"
    argv = (
        "intake", "explore", repo,
        "--intent", "Document how retries stop",
        "--source-ref", "user:thread-1",
        "--authority", "planning",
        "--assessment-command", command,
        "--write", "--json",
    )

    first = run_go(*argv, env=env)
    assert first.returncode == 0, first.stderr + first.stdout
    result = json.loads(first.stdout)
    assert result["status"] == "applied"
    assert result["task_ids"] == ["document-retries"]
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / "schemas/intake-assessment.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    record = json.loads(next((repo / ".go/intake").glob("*.json")).read_text())
    Draft202012Validator(schema).validate(record["assessment"])
    task = json.loads((repo / ".go/tasks/open/document-retries.json").read_text())
    assert task["requested_outcomes"] == [
        {"id": "R1", "text": "Document how retries stop", "source": "user:thread-1#O1",
         "status": "pending", "evidence": []}
    ]
    assert task["intake"]["authority"]["mode"] == "planning"
    request = json.loads(capture.read_text().splitlines()[0])
    assert request["phase"] == "critic"
    assert request["task"]["intake_request"]["intent"]["text"] == "Document how retries stop"

    second = run_go(*argv, env=env)
    assert second.returncode == 0, second.stderr + second.stdout
    assert json.loads(second.stdout)["status"] == "already_applied"
    assert len(capture.read_text().splitlines()) == 1
    assert len(list((repo / ".go/tasks/open").glob("document-retries*.json"))) == 1


def test_planning_task_cannot_be_claimed_as_execution_authority(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    explored = run_go(
        "intake", "explore", repo, "--intent", "Document how retries stop",
        "--source-ref", "user:planning-only", "--authority", "planning",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert explored.returncode == 0, explored.stderr

    claimed = run_go("claim", "document-retries", "--repo", repo, "--agent", "fixture")
    assert claimed.returncode != 0
    assert "planning" in claimed.stderr and "execution authority" in claimed.stderr
    assert (repo / ".go/tasks/open/document-retries.json").is_file()


def test_unknown_bug_root_cause_cannot_materialize_a_fix(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    script.write_text(
        script.read_text()
        .replace("'work_type': 'implementation'", "'work_type': 'bug_fix'")
        .replace("'root_cause': 'not_applicable'", "'root_cause': 'unknown'")
    )
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    result = run_go(
        "intake", "explore", repo, "--intent", "Fix the intermittent retry jump",
        "--source-ref", "user:bug-report", "--authority", "execute",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode != 0
    assert "unknown bug root cause" in result.stderr and "research" in result.stderr
    assert not (repo / ".go/tasks/open/document-retries.json").exists()
    assert not (repo / ".go/intake").exists()


def test_assessment_cannot_resurrect_a_superseded_decision(tmp_path):
    repo = seed(tmp_path)
    decisions = repo / ".go/decisions/events.jsonl"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    def decision(status):
        return {"schema": "go-workflow.repo-local.event.v1", "kind": "event", "event": "decision.recorded",
                "created_at": "2026-09-21T20:00:00Z", "task_id": "decision-fixture", "agent": "fixture",
                "data": {"decision_id": "retry-rule", "title": "Retry rule", "status": status}}
    decisions.write_text("\n".join(map(json.dumps, [decision("accepted"), decision("superseded")])) + "\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "decision history"], cwd=repo, check=True)
    script = adapter(tmp_path)
    script.write_text(script.read_text().replace(
        "'questions': [], 'relevant_decisions': [],",
        "'questions': [], 'relevant_decisions': [{'id': 'retry-rule', 'status': 'accepted'}],",
    ))
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}

    result = run_go(
        "intake", "explore", repo, "--intent", "Document how retries stop",
        "--source-ref", "user:thread-1", "--authority", "execute",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode != 0
    assert "superseded" in result.stderr and "retry-rule" in result.stderr
    assert not (repo / ".go/tasks/open/document-retries.json").exists()


def test_new_feedback_updates_existing_open_work_without_losing_prior_source(tmp_path):
    repo = seed(tmp_path)
    first_adapter = adapter(tmp_path)
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    first = run_go(
        "intake", "explore", repo, "--intent", "Document how retries stop",
        "--source-ref", "user:thread-1", "--authority", "planning",
        "--assessment-command", f"{sys.executable} {first_adapter}", "--write", "--json", env=env,
    )
    assert first.returncode == 0, first.stderr
    update_adapter = tmp_path / "update.py"
    update_adapter.write_text(
        first_adapter.read_text()
        .replace("'action': 'create'", "'action': 'update'")
        .replace("'Retry states and stop behavior are documented'", "'Jitter backoff is documented'")
        .replace("'test -f docs/retries.md'", "'test -f docs/jitter.md'")
        .replace("'modify': ['docs/retries.md']", "'modify': ['docs/jitter.md']")
    )

    second = run_go(
        "intake", "explore", repo, "--intent", "Also document jitter backoff",
        "--source-ref", "user:feedback-2", "--authority", "planning",
        "--assessment-command", f"{sys.executable} {update_adapter}", "--write", "--json", env=env,
    )
    assert second.returncode == 0, second.stderr + second.stdout
    task = json.loads((repo / ".go/tasks/open/document-retries.json").read_text())
    assert [(item["text"], item["source"]) for item in task["requested_outcomes"]] == [
        ("Document how retries stop", "user:thread-1#O1"),
        ("Also document jitter backoff", "user:feedback-2#O1"),
    ]
    assert task["intent_source"]["source_ref"] == "user:thread-1"
    assert [item["source_ref"] for item in task["intake_history"]] == ["user:thread-1", "user:feedback-2"]
    assert task["acceptance"] == ["Retry states and stop behavior are documented", "Jitter backoff is documented"]
    assert task["verification"] == ["test -f docs/retries.md", "test -f docs/jitter.md"]
    assert task["scope"]["modify"] == ["docs/retries.md", "docs/jitter.md"]


def test_affected_task_question_must_be_persisted_not_invented_by_action(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    script.write_text(script.read_text().replace("'question_ids': []", "'question_ids': ['Q1']"))
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    result = run_go(
        "intake", "explore", repo, "--intent", "Document how retries stop",
        "--source-ref", "user:thread-1", "--authority", "execute",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode != 0
    assert "unknown question" in result.stderr and "Q1" in result.stderr
    assert not (repo / ".go/tasks/open/document-retries.json").exists()


def test_user_question_blocks_only_affected_task(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    marker = "print(json.dumps({'schema': 'go-workflow.agent-adapter-result.v1'"
    injection = """assessment['disposition'] = 'questions'
assessment['actions'][0]['question_ids'] = ['Q1']
independent = dict(assessment['actions'][0])
independent.update(task_id='document-backoff', summary='Document backoff', question_ids=[])
assessment['actions'].append(independent)
assessment['questions'] = [{'id': 'Q1', 'text': 'Which stop code is canonical?',
                            'owner': 'user', 'blocks': ['document-retries']}]
"""
    script.write_text(script.read_text().replace(marker, injection + marker))
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    result = run_go(
        "intake", "explore", repo, "--intent", "Document retry and backoff behavior",
        "--source-ref", "user:thread-questions", "--authority", "execute",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["questions"][0]["id"] == "Q1"

    affected = run_go("claim", "document-retries", "--repo", repo, "--agent", "fixture")
    assert affected.returncode != 0 and "Q1" in affected.stderr
    independent = run_go("claim", "document-backoff", "--repo", repo, "--agent", "fixture")
    assert independent.returncode == 0, independent.stderr


def test_validate_rejects_intake_record_drift(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    applied = run_go(
        "intake", "explore", repo, "--intent", "Document how retries stop",
        "--source-ref", "user:thread-1", "--authority", "planning",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert applied.returncode == 0, applied.stderr
    record_path = next((repo / ".go/intake").glob("*.json"))
    record = json.loads(record_path.read_text())
    record["assessment"]["request_sha256"] = "0" * 64
    record_path.write_text(json.dumps(record))

    checked = run_go("validate", repo)
    assert checked.returncode != 0
    assert "intake" in checked.stderr.lower() and "binding" in checked.stderr.lower()


def test_assessment_process_cannot_mutate_target_repository(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    script.write_text(script.read_text().replace(
        "request = json.loads(os.environ['GO_ADAPTER_REQUEST_JSON'])",
        "request = json.loads(os.environ['GO_ADAPTER_REQUEST_JSON'])\nPath('adapter-write.txt').write_text('must stay isolated')",
    ))
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    result = run_go(
        "intake", "explore", repo, "--intent", "Document how retries stop",
        "--source-ref", "user:thread-1", "--authority", "planning",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert not (repo / "adapter-write.txt").exists()
    assert (repo / ".go/tasks/open/document-retries.json").is_file()


def test_multi_part_intent_preserves_each_original_outcome_without_forcing_task_split(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    script.write_text(script.read_text().replace("'outcome_ids': ['O1']", "'outcome_ids': ['O1', 'O2']"))
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    intent = "Improve retry docs\n1. Explain stop states\n2. Explain jitter backoff"
    result = run_go(
        "intake", "explore", repo, "--intent", intent,
        "--source-ref", "user:multi-part", "--authority", "planning",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    task = json.loads((repo / ".go/tasks/open/document-retries.json").read_text())
    assert [(item["text"], item["source"]) for item in task["requested_outcomes"]] == [
        ("Explain stop states", "user:multi-part#O1"),
        ("Explain jitter backoff", "user:multi-part#O2"),
    ]
    assert json.loads(result.stdout)["task_ids"] == ["document-retries"]


def test_assessment_cannot_silently_drop_one_requested_outcome(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    result = run_go(
        "intake", "explore", repo,
        "--intent", "Requested work\n1. Explain stop states\n2. Explain jitter backoff",
        "--source-ref", "user:missing-outcome", "--authority", "planning",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode != 0
    assert "does not preserve requested outcome: O2" in result.stderr
    assert not (repo / ".go/tasks/open/document-retries.json").exists()


def test_invalid_later_action_does_not_leave_partial_tasks(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    marker = "print(json.dumps({'schema': 'go-workflow.agent-adapter-result.v1'"
    injection = """collision = dict(assessment['actions'][0])
collision['task_id'] = 'task-schema-smoke'
assessment['actions'].append(collision)
"""
    script.write_text(script.read_text().replace(marker, injection + marker))
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    result = run_go(
        "intake", "explore", repo, "--intent", "Document how retries stop",
        "--source-ref", "user:thread-1", "--authority", "execute",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode != 0 and "collides" in result.stderr
    assert not (repo / ".go/tasks/open/document-retries.json").exists()
    assert not (repo / ".go/intake").exists()


def test_native_codex_assessment_enforces_selected_model_and_persists_usage(tmp_path):
    repo = seed(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "codex"
    binary.write_text("#!" + sys.executable + """
import json, os, sys
if sys.argv[1] == 'app-server':
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        result = {} if request['method'] == 'initialize' else {
            'data': [{'id': 'gpt-6-astra', 'model': 'gpt-6-astra',
                      'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}],
            'nextCursor': None,
        }
        print(json.dumps({'id': request['id'], 'result': result}), flush=True)
else:
    request = json.loads(os.environ['GO_ADAPTER_REQUEST_JSON'])
    intake = request['task']['intake_request']
    assessment = {
        'schema': 'go-workflow.intake-assessment.v1', 'request_sha256': intake['sha256'],
        'summary': 'Native bounded assessment.', 'disposition': 'ready',
        'actions': [{'action': 'create', 'task_id': 'native-intake-task', 'summary': 'Native intake task',
                     'work_type': 'research', 'root_cause': 'not_applicable', 'substantial': False,
                     'outcome_ids': ['O1'], 'scope': {'read': ['docs/**'], 'modify': ['docs/native.md']},
                     'behavior': {}, 'non_goals': [], 'dependencies': [], 'delegated_choices': [],
                     'question_ids': [], 'acceptance': ['Research is documented'],
                     'verification': ['test -f docs/native.md']}],
        'questions': [], 'relevant_decisions': [],
    }
    message = json.dumps({'schema': 'go-workflow.agent-adapter-result.v1', 'phase': 'critic',
                          'status': 'success', 'summary': 'Native assessment complete',
                          'intake_assessment': assessment})
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': message}}))
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12, 'output_tokens': 4}}))
""")
    binary.chmod(0o755)
    env = {**os.environ, "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"]}
    result = run_go(
        "intake", "explore", repo, "--intent", "Research native intake",
        "--source-ref", "user:native", "--authority", "planning",
        "--executor-agent", "codex", "--model", "gpt-6-astra", "--effort", "high",
        "--write", "--json", env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    record = json.loads(next((repo / ".go/intake").glob("*.json")).read_text())
    assert record["adapter"]["model_selection"]["requested"] == {"id": "gpt-6-astra", "effort": "high"}
    assert record["adapter"]["usage"]["turns"] == [{"input_tokens": 12, "output_tokens": 4}]


def test_new_intake_dependencies_must_use_enforceable_lifecycle_edges(tmp_path):
    repo = seed(tmp_path)
    script = adapter(tmp_path)
    script.write_text(script.read_text().replace("'dependencies': []", "'dependencies': ['research in prose']"))
    env = {**os.environ, "INTAKE_CAPTURE": str(tmp_path / "capture.jsonl")}
    result = run_go(
        "intake", "explore", repo, "--intent", "Document how retries stop",
        "--source-ref", "user:thread-1", "--authority", "execute",
        "--assessment-command", f"{sys.executable} {script}", "--write", "--json", env=env,
    )
    assert result.returncode != 0
    assert "dependency" in result.stderr and "object" in result.stderr
    assert not (repo / ".go/tasks/open/document-retries.json").exists()
