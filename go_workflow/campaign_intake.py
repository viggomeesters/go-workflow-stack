"""Read-only materialization of explicitly authorized taskwise campaigns.

Optional project.json ``taskwise_policy`` accepts boolean ``allow_push`` and
``allow_deployment`` restrictions. It cannot enlarge the selected task scope or
invent a release/deployment configuration. Persisting the returned contract is
owned by the caller; this function never modifies existing runs or task records.
Shipping restrictions are captured in execution.shipping, including the difference
between no commit authority and local-only commit authority.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .architecture import latest_decisions
from .campaign_contracts import CAMPAIGN_SCHEMA, STOP_CONDITIONS, campaign_findings
from .execution_contracts import validate_execution_contract
from .worktrees import workflow_root


def _read(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be explicitly configured as nonempty text')
    return value


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def materialize_until_scope(repo: Path, *, intent: str, source_ref: str,
                            campaign_id: str, task_ids=None, budget=None, ship_policy=None) -> dict:
    """Freeze existing unfinished outcomes under the caller's execution authority."""
    if ship_policy not in (None, 'none', 'local-commit', 'push'):
        raise ValueError('Invalid explicit ship_policy')
    _text(intent, 'intent')
    _text(source_ref, 'current execution authorization source_ref')
    root = workflow_root(Path(repo).resolve())
    project = _read(root / 'project.json')
    policy = project.get('taskwise_policy', {})
    if (not isinstance(policy, dict) or set(policy) - {'allow_push', 'allow_deployment'}
            or any(type(value) is not bool for value in policy.values())):
        raise ValueError('taskwise_policy accepts only boolean allow_push and allow_deployment')
    effective_shipping = ship_policy or 'push'
    if not policy.get('allow_push', True) and effective_shipping == 'push':
        effective_shipping = 'local-commit'
    tasks = {}
    for state in ('open', 'active', 'blocked', 'done'):
        for path in sorted((root / 'tasks' / state).glob('*.json')):
            task = _read(path)
            identity = _text(task.get('id'), f'{path}: task id')
            if identity in tasks:
                raise ValueError(f'duplicate task state: {identity}')
            if task.get('status') != state or path.stem != identity:
                raise ValueError(f'task identity/state mismatch: {path}')
            tasks[identity] = task
    if task_ids is None:
        selected = [key for key, task in tasks.items() if task['status'] != 'done']
    else:
        if not isinstance(task_ids, list) or any(not isinstance(key, str) for key in task_ids):
            raise ValueError('task_ids must be a list of existing unfinished task IDs')
        selected = list(task_ids)
        if len(selected) != len(set(selected)):
            raise ValueError('task_ids contains duplicate IDs')
        for key in selected:
            if key not in tasks or tasks[key]['status'] == 'done':
                raise ValueError(f'task must exist and be unfinished: {key}')
    if not selected:
        raise ValueError('No unfinished tasks to adopt; audit existing outcomes instead of creating an empty campaign')
    profiles, models, outcomes, targets = [], [], [], []
    configured = project.get('release_profiles', {})
    for key in selected:
        task = tasks[key]
        execution = task.get('execution_contract')
        errors = validate_execution_contract(execution)
        if errors:
            raise ValueError(f'{key}: configure execution_contract before campaign intake: ' + '; '.join(errors))
        for field in ('model', 'critic_model'):
            if field in execution and execution[field] not in models:
                models.append(dict(execution[field]))
        evidence = ['verification', 'critic']
        release = execution['release']
        if release['mode'] == 'required':
            profile = release.get('profile')
            if not profile or not isinstance(configured.get(profile), dict):
                raise ValueError(f'{key}: configure required release profile {profile!r}')
            if profile not in profiles:
                profiles.append(profile)
            evidence.append('release')
            deployment = configured[profile].get('deployment')
            if isinstance(deployment, dict) and deployment.get('mode') == 'required':
                target = _text(deployment.get('target'), f'{key}: deployment target')
                evidence.append('live')
                if effective_shipping == 'push' and policy.get('allow_deployment', True) and target not in targets:
                    targets.append(target)
        requested = task.get('requested_outcomes')
        if not isinstance(requested, list) or not requested:
            raise ValueError(f'{key}: requested_outcomes with original R# text are required')
        for requirement in requested:
            if not isinstance(requirement, dict):
                raise ValueError(f'{key}: malformed requested outcome')
            text = _text(requirement.get('text'), f'{key}: original outcome text')
            outcomes.append({'id': f'O{len(outcomes) + 1}', 'text': text,
                             'task_outcomes': [{'task_id': key, 'requirement_id': requirement.get('id'),
                                                'text_sha256': _sha(text)}],
                             'required_evidence': list(evidence)})
    decisions = []
    for identity, event in sorted(latest_decisions(root).items()):
        actual = event['data']
        if actual.get('status') != 'accepted':
            continue
        decisions.append({'id': identity, 'status': 'accepted',
                          'source_ref': f'.go/decisions/events.jsonl#{identity}',
                          'owner': _text(event.get('agent'), f'decision {identity}: recorded owner'),
                          'resolution_gate': f'accepted decision {identity}',
                          'task_ids': [],
                          'bounds': _text(actual.get('decision'), f'decision {identity}: recorded decision text')})
    if not decisions:
        raise ValueError('No accepted repository decision; record the actual governing decision before intake')
    ceilings = {'wall_seconds': None, 'max_tasks': None, 'max_attempts': None}
    if budget is not None:
        if not isinstance(budget, dict) or set(budget) - (set(ceilings) | {'max_commands'}):
            raise ValueError('budget accepts only wall_seconds, max_tasks and max_attempts')
        ceilings.update(budget)
    repair_scope = sorted({pattern for key in selected for pattern in tasks[key]['scope']['modify']})
    push = effective_shipping == 'push'
    result = {'schema': CAMPAIGN_SCHEMA, 'id': campaign_id, 'project': project['id'],
              'revision': 1, 'previous_sha256': None,
              'execution': {'schema': 'go-workflow.taskwise-execution.v1', 'mode': 'until_scope',
                            'shipping': {'schema': 'go-workflow.taskwise-shipping.v1',
                                         'policy': effective_shipping, 'source_ref': source_ref}},
              'intent': {'text': intent, 'sha256': _sha(intent), 'source_ref': source_ref},
              'goal': {'text': intent, 'non_goals': [], 'outcomes': outcomes},
              'basis': {'vision_sha256': hashlib.sha256((root / 'vision.json').read_bytes()).hexdigest(),
                        'principles_sha256': hashlib.sha256((root / 'architecture-principles.json').read_bytes()).hexdigest(),
                        'decision_ids': [item['id'] for item in decisions]},
              'authority': {'mode': 'execute', 'source_ref': source_ref, 'permitted_tasks': selected,
                            'expansion': {'research': {'max_tasks': 0, 'modify': [], 'outcome_ids': []},
                                          'repair': {'max_tasks': len(selected) if repair_scope else 0,
                                                     'modify': repair_scope,
                                                     'outcome_ids': [outcome['id'] for outcome in outcomes] if repair_scope else []}},
                            'models': models, 'budget': ceilings,
                            'release': {'profiles': profiles, 'allow_push': push,
                                        'source_ref': source_ref if profiles or push else None},
                            'deployment': {'targets': targets, 'source_ref': source_ref if targets else None},
                            'stop_conditions': list(STOP_CONDITIONS)}, 'decisions': decisions}
    errors = campaign_findings(Path(repo), result)
    if errors:
        raise ValueError('Campaign intake is not executable: ' + '; '.join(errors))
    return result
