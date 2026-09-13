"""Phase definitions and evidence are distinct from skill metadata."""
import json
import pytest

from test_abc_contracts import fixture, run, write


def test_project_rejects_phase_without_required_handoff_and_outcome_contract(tmp_path):
    repo = fixture(tmp_path)
    p = repo / '.go/project.json'
    project = json.loads(p.read_text())
    project['phase_profiles'] = {'standard': [{'schema': 'go-workflow.phase-contract.v1', 'id': 'verify'}]}
    write(p, project)
    result = run(repo, 'validate', repo)
    assert result.returncode != 0 and 'phase' in result.stderr


@pytest.mark.parametrize('status,extra,valid', [
    ('missing', {}, True),
    ('not_applicable', {}, False),
    ('not_applicable', {'reason': 'Configured source smoke fixture'}, True),
    ('passed', {}, False),
    ('passed', {'command': 'pytest -q', 'cwd': '.', 'revision': 'a' * 40,
                'worktree_digest': 'clean', 'exit_code': 0, 'evidence': ['fixture:test.log']}, True),
    ('passed', {'command': 'pytest -q', 'cwd': '.', 'revision': 'a' * 40,
                'worktree_digest': 'clean', 'exit_code': 1, 'evidence': ['fixture:test.log']}, False),
])
def test_evidence_schema_and_repo_validator_agree(tmp_path, status, extra, valid):
    from jsonschema import Draft202012Validator
    from test_abc_contracts import ROOT
    repo = fixture(tmp_path)
    proof = {'schema': 'go-workflow.verification-evidence.v1', 'task_id': 'task-schema-smoke',
             'phase_id': 'verify', 'requirement_ids': ['R1'], 'status': status, **extra}
    schema = json.loads((ROOT / 'schemas/verification-evidence.schema.json').read_text())
    assert Draft202012Validator(schema).is_valid(proof) == valid
    path = repo / '.go/tasks/open/task-schema-smoke.json'
    task = json.loads(path.read_text()); task['verification_evidence'] = [proof]; write(path, task)
    result = run(repo, 'validate', repo)
    assert (result.returncode == 0) == valid, result.stdout + result.stderr
