"""Dependency identity for reusable managed proof, distinct from dispatch context."""
from pathlib import Path

from . import completion
from .execution_context import canonical_sources, file_state, git_state, json_hash, selected_files


def proof_identity(workspace, record, task, models):
    workspace = Path(workspace)
    control = Path(record['control_repo'])
    git = git_state(workspace, record)
    sources = [p for p in canonical_sources(control / '.go', task)
               if p.parent.name != 'active' and p.name != 'hierarchy.json']
    return json_hash({
        'schema': 'go-workflow.proof-dependencies.v1',
        'candidate': {key: git[key] for key in ('head', 'base_commit', 'branch')},
        'content': completion.content_snapshot(workspace, task)['digest'],
        'contract': completion.contract_digest(task), 'models': models,
        'authority': {str(p.relative_to(control)): file_state(p) for p in sources},
        'instructions': {phase: selected_files(workspace, task, phase)
                         for phase in ('build', 'critic', 'repair')},
    })


def valid_check_prefix(workspace, task, state):
    """Revalidate immutable command receipts before carrying a confirmed prefix."""
    checks = state['checks']
    if not isinstance(checks, list):
        return False
    if len(checks) != state['check_index'] or len(checks) > len(task['verification']):
        return False
    binding = completion.bind(workspace, task)
    root = completion.workflow_root(workspace)
    try:
        for command, output in zip(task['verification'], checks):
            if not isinstance(output, dict) or output.get('returncode') != 0:
                return False
            check = output['completion_check']
            if not isinstance(check, dict):
                return False
            proof = check['verification']
            if not isinstance(proof, dict):
                return False
            raw = completion.read_artifact(root, check['raw'])
            if (completion.validate_verification_evidence(proof) or proof.get('status') != 'passed'
                    or proof.get('task_id') != task['id'] or proof.get('phase_id') != 'verify'
                    or proof.get('requirement_ids') != ([item['id'] for item in task.get('requested_outcomes', [])] or ['acceptance'])
                    or proof.get('evidence') != [check['raw']['path']]
                    or raw.get('cwd') != proof.get('cwd') or raw.get('timed_out') is not False
                    or type(raw.get('returncode')) is not int
                    or proof.get('command') != command or proof.get('exit_code') != 0
                    or proof.get('revision') != binding['revision']
                    or proof.get('worktree_digest') != binding['content_digest']
                    or raw.get('command') != command or raw.get('returncode') != 0
                    or any(raw.get(key) != value for key, value in binding.items())):
                return False
        return True
    except (ValueError, OSError, KeyError, TypeError):
        return False


def valid_checks(workspace, task, checks):
    """Validate a supplied confirmed prefix without requiring a managed-state record."""
    return isinstance(checks, list) and valid_check_prefix(workspace, task, {'checks': checks, 'check_index': len(checks)})
