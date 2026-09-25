"""Read-only, opt-in bounds for serial campaigns; task state remains authoritative.

Validation is neither user authorization nor completion proof. The controller
must retain source evidence and apply the existing task/architecture/release gates.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .execution_contracts import EFFORTS, ID_RE, validate_execution_contract

CAMPAIGN_SCHEMA = 'go-workflow.campaign-contract.v1'
STOP_CONDITIONS = ('goal_verified', 'budget_exhausted', 'no_eligible_tasks',
                   'authority_required', 'unsafe_repository', 'unknown_external_effect')
EVIDENCE_KINDS = ('verification', 'critic', 'release', 'live')
SHA_RE = re.compile(r'^[0-9a-f]{64}$')


def contract_digest(data: dict[str, Any]) -> str:
    """Canonical JSON digest, independent of file indentation and key order."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def validate_campaign_contract(data: Any) -> list[str]:
    """Validate shape and internal references without reading or writing a repo."""
    errors: list[str] = []

    def obj(value, keys, label):
        if not isinstance(value, dict):
            errors.append(f'{label} must be an object')
            return {}
        if set(value) != set(keys.split()):
            errors.append(f'{label} has missing or unknown fields')
        return value

    def string(value, label, pattern=None):
        valid = isinstance(value, str) and bool(value.strip())
        if pattern is not None:
            valid = valid and bool(pattern.fullmatch(value))
        if not valid:
            errors.append(f'{label} invalid')
        return valid

    def integer(value, label, minimum=1):
        if type(value) is not int or value < minimum:
            errors.append(f'{label} must be an integer >= {minimum}')

    def array(value, label, minimum=0):
        if not isinstance(value, list) or len(value) < minimum:
            errors.append(f'{label} must be a list with >= {minimum} items')
            return []
        return value

    def strings(value, label, minimum=0, pattern=None, choices=None):
        values = array(value, label, minimum)
        seen = set()
        for item in values:
            if string(item, label, pattern):
                if choices is not None and item not in choices:
                    errors.append(f'{label} invalid choice: {item}')
                if item in seen:
                    errors.append(f'{label} duplicate: {item}')
                seen.add(item)
        return seen

    taskwise = isinstance(data, dict) and 'execution' in data
    data = obj(data, 'schema id project revision previous_sha256 intent goal basis authority decisions' + (' execution' if taskwise else ''), 'campaign')
    if taskwise:
        has_shipping = isinstance(data['execution'], dict) and 'shipping' in data['execution']
        execution = obj(data['execution'], 'schema mode' + (' shipping' if has_shipping else ''), 'execution')
        if has_shipping:
            shipping = obj(execution['shipping'], 'schema policy source_ref', 'shipping')
            if shipping.get('schema') != 'go-workflow.taskwise-shipping.v1' or shipping.get('policy') not in {'none', 'local-commit', 'push'}:
                errors.append('invalid taskwise shipping policy')
            string(shipping.get('source_ref'), 'shipping.source_ref')
        if execution.get('schema') != 'go-workflow.taskwise-execution.v1' or execution.get('mode') != 'until_scope':
            errors.append('execution must explicitly select taskwise until_scope')
    if data.get('schema') != CAMPAIGN_SCHEMA:
        errors.append('campaign schema mismatch')
    for key in ('id', 'project'):
        string(data.get(key), key, ID_RE)
    integer(data.get('revision'), 'revision')
    if data.get('revision') == 1:
        if data.get('previous_sha256') is not None:
            errors.append('first revision cannot have previous_sha256')
    else:
        string(data.get('previous_sha256'), 'previous_sha256', SHA_RE)
    intent = obj(data.get('intent'), 'text sha256 source_ref', 'intent')
    for key in ('text', 'source_ref'):
        string(intent.get(key), f'intent.{key}')
    string(intent.get('sha256'), 'intent.sha256', SHA_RE)
    if isinstance(intent.get('text'), str) and hashlib.sha256(intent['text'].encode()).hexdigest() != intent.get('sha256'):
        errors.append('intent hash mismatch')
    goal = obj(data.get('goal'), 'text non_goals outcomes', 'goal')
    string(goal.get('text'), 'goal.text')
    strings(goal.get('non_goals'), 'goal.non_goals')
    outcome_ids, linked_tasks = set(), set()
    outcomes = array(goal.get('outcomes'), 'goal.outcomes', 1)
    for raw in outcomes:
        outcome = obj(raw, 'id text task_outcomes required_evidence', 'outcome')
        if string(outcome.get('id'), 'outcome.id', ID_RE):
            if outcome['id'] in outcome_ids:
                errors.append('duplicate outcome id')
            outcome_ids.add(outcome['id'])
        string(outcome.get('text'), 'outcome.text')
        evidence = strings(outcome.get('required_evidence'), 'outcome.required_evidence', 1, choices=EVIDENCE_KINDS)
        if not {'verification', 'critic'} <= evidence:
            errors.append('outcome requires verification and critic evidence')
        links = array(outcome.get('task_outcomes'), 'outcome.task_outcomes')
        seen = set()
        for raw_link in links:
            link = obj(raw_link, 'task_id requirement_id text_sha256', 'task outcome link')
            string(link.get('text_sha256'), 'task outcome text_sha256', SHA_RE)
            valid_id = string(link.get('task_id'), 'task_id', ID_RE)
            valid_req = string(link.get('requirement_id'), 'requirement_id', re.compile(r'^R[1-9][0-9]*$'))
            if valid_id:
                linked_tasks.add(link['task_id'])
            if valid_id and valid_req:
                key = (link['task_id'], link['requirement_id'])
                if key in seen:
                    errors.append('duplicate task outcome link')
                seen.add(key)
    basis = obj(data.get('basis'), 'vision_sha256 principles_sha256 decision_ids', 'basis')
    for key in ('vision_sha256', 'principles_sha256'):
        string(basis.get(key), f'basis.{key}', SHA_RE)
    governing = strings(basis.get('decision_ids'), 'basis.decision_ids', 1, ID_RE)
    authority = obj(data.get('authority'), 'mode source_ref permitted_tasks expansion models budget release deployment stop_conditions', 'authority')
    mode = authority.get('mode')
    if mode not in ('planning', 'execute'):
        errors.append('authority.mode invalid')
    string(authority.get('source_ref'), 'authority.source_ref')
    permitted = strings(authority.get('permitted_tasks'), 'permitted tasks', pattern=ID_RE)
    if linked_tasks - permitted:
        errors.append('outcome links must reference permitted tasks')
    if mode == 'execute':
        if not permitted:
            errors.append('execute requires permitted tasks')
        for outcome in outcomes:
            if isinstance(outcome, dict) and not outcome.get('task_outcomes'):
                errors.append('execute cannot have unmapped outcomes')
        if permitted - linked_tasks:
            errors.append('every permitted task must map to an outcome')
    expansion = obj(authority.get('expansion'), 'research repair', 'expansion')
    for kind in ('research', 'repair'):
        entry = obj(expansion.get(kind), 'max_tasks modify outcome_ids', f'expansion.{kind}')
        integer(entry.get('max_tasks'), f'expansion.{kind}.max_tasks', 0)
        paths = strings(entry.get('modify'), f'expansion.{kind}.modify')
        for path in paths:
            if path.startswith('/') or '\\' in path or ':' in path or '..' in path.split('/'):
                errors.append('expansion modify paths must be repository-relative without traversal')
        refs = strings(entry.get('outcome_ids'), f'expansion.{kind}.outcome_ids', pattern=ID_RE)
        if refs - outcome_ids:
            errors.append('expansion references unknown outcomes')
        if type(entry.get('max_tasks')) is int:
            if entry['max_tasks'] > 0 and (not paths or not refs):
                errors.append('enabled expansion requires modify bounds and outcome_ids')
            if entry['max_tasks'] == 0 and (paths or refs):
                errors.append('disabled expansion must have empty bounds')
    seen_models = set()
    for raw in array(authority.get('models'), 'models', 1):
        model = obj(raw, 'id effort', 'model')
        valid = string(model.get('id'), 'model.id')
        if model.get('effort') not in EFFORTS:
            errors.append('model.effort invalid')
        elif valid:
            key = (model['id'], model['effort'])
            if key in seen_models:
                errors.append('duplicate model')
            seen_models.add(key)
    budget_keys = 'wall_seconds max_tasks max_attempts'
    if taskwise and isinstance(authority.get('budget'), dict) and 'max_commands' in authority['budget']:
        budget_keys += ' max_commands'
    budget = obj(authority.get('budget'), budget_keys, 'budget')
    if 'max_commands' in budget:
        integer(budget['max_commands'], 'budget.max_commands')
    for key in ('wall_seconds', 'max_tasks', 'max_attempts'):
        if not (taskwise and budget.get(key) is None):
            integer(budget.get(key), f'budget.{key}')
    release = obj(authority.get('release'), 'profiles allow_push source_ref', 'release')
    profiles = strings(release.get('profiles'), 'release.profiles', pattern=ID_RE)
    if type(release.get('allow_push')) is not bool:
        errors.append('release.allow_push must be boolean')
    deployment = obj(authority.get('deployment'), 'targets source_ref', 'deployment')
    targets = strings(deployment.get('targets'), 'deployment.targets')
    for label, section, enabled in [('release', release, profiles or release.get('allow_push') is True),
                                    ('deployment', deployment, targets)]:
        if enabled:
            string(section.get('source_ref'), f'{label} authority source_ref')
            if mode == 'planning':
                errors.append(f'planning cannot grant {label} authority')
        elif section.get('source_ref') is not None:
            errors.append(f'disabled {label} authority must have null source_ref')
    if release.get('allow_push') is True and not profiles and not taskwise:
        errors.append('release authority for push requires explicit profiles')
    shipping = (data.get('execution') or {}).get('shipping')
    if shipping and (shipping.get('policy') == 'push') != (release.get('allow_push') is True):
        errors.append('shipping policy and push authority disagree')
    stops = strings(authority.get('stop_conditions'), 'stop_conditions', len(STOP_CONDITIONS), choices=STOP_CONDITIONS)
    if stops != set(STOP_CONDITIONS):
        errors.append('all mandatory stop_conditions required')
    accepted, decision_ids = set(), set()
    for raw in array(data.get('decisions'), 'decisions', 1):
        decision = obj(raw, 'id status source_ref owner resolution_gate task_ids bounds', 'decision')
        if string(decision.get('id'), 'decision.id', ID_RE):
            if decision['id'] in decision_ids:
                errors.append('duplicate decision id')
            decision_ids.add(decision['id'])
            if decision.get('status') == 'accepted':
                accepted.add(decision['id'])
        if decision.get('status') not in ('accepted', 'proposed', 'delegated', 'unresolved'):
            errors.append('decision.status invalid')
        for key in ('source_ref', 'owner', 'resolution_gate', 'bounds'):
            string(decision.get(key), f'decision.{key}')
        refs = strings(decision.get('task_ids'), 'decision.task_ids', pattern=ID_RE)
        if refs - permitted:
            errors.append('decision references tasks outside permitted tasks')
        if decision.get('status') == 'delegated' and decision.get('source_ref') != authority.get('source_ref'):
            errors.append('delegated decision must cite existing campaign authority')
    if governing - accepted:
        errors.append('basis requires exact accepted decision references')
    return errors


def _revision_findings(data: dict, previous: Any) -> list[str]:
    if data['revision'] == 1:
        return ['first revision cannot replace a previous contract'] if previous is not None else []
    if previous is None:
        return ['previous contract required to verify revision continuity']
    errors = validate_campaign_contract(previous)
    if errors:
        return ['previous contract invalid: ' + '; '.join(errors)]
    if data['revision'] != previous['revision'] + 1:
        errors.append('revision must increment by one')
    if data['previous_sha256'] != contract_digest(previous):
        errors.append('previous contract digest mismatch')
    if any(data[key] != previous[key] for key in ('id', 'project', 'intent')):
        errors.append('campaign identity and original intent cannot be rewritten')
    current = {o['id']: o for o in data['goal']['outcomes']}
    for old in previous['goal']['outcomes']:
        new = current.get(old['id'])
        if (new is None or new['text'] != old['text']
                or not set(old['required_evidence']) <= set(new['required_evidence'])):
            errors.append(f'original outcome/evidence cannot be removed or weakened: {old["id"]}')
    return errors


def campaign_task_findings(data: Any, task: dict[str, Any]) -> list[str]:
    """Static mandate restrictions, NOT a replacement for runtime readiness gates.

    New research/repair tasks require a controller-admitted contract revision;
    an expansion allowance alone never makes an unlisted task executable.
    """
    errors = validate_campaign_contract(data)
    if errors:
        return errors
    authority = data['authority']
    if authority['mode'] != 'execute':
        errors.append('planning authority cannot execute tasks')
    if task.get('id') not in authority['permitted_tasks']:
        errors.append('task is outside permitted tasks')
    for decision in data['decisions']:
        if task.get('id') in decision['task_ids'] and decision['status'] in ('proposed', 'unresolved'):
            errors.append(f'{decision["status"]} decision {decision["id"]}: owner={decision["owner"]}; gate={decision["resolution_gate"]}')
    contract = task.get('execution_contract')
    errors.extend(validate_execution_contract(contract))
    if isinstance(contract, dict):
        for key in ('model', 'critic_model'):
            if key in contract and contract[key] not in authority['models']:
                errors.append(f'task {key} outside campaign model profiles')
        release = contract.get('release')
        if (isinstance(release, dict) and release.get('mode') == 'required'
                and release.get('profile') not in authority['release']['profiles']):
            errors.append('task release profile outside campaign authority')
    return errors


def campaign_findings(repo: Path, data: Any, *, previous: Any = None) -> list[str]:
    """Check a proposed contract against canonical references, with no mutations."""
    from .architecture import latest_decisions
    from .worktrees import workflow_root

    errors = validate_campaign_contract(data)
    if errors:
        return errors
    errors.extend(_revision_findings(data, previous))
    root = workflow_root(repo.resolve())

    def read(path):
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError(f'object required: {path}')
        return value

    try:
        project = read(root / 'project.json')
        if data['project'] != project.get('id'):
            errors.append('campaign project identity mismatch')
        for field, filename in [('vision_sha256', 'vision.json'), ('principles_sha256', 'architecture-principles.json')]:
            if hashlib.sha256((root / filename).read_bytes()).hexdigest() != data['basis'][field]:
                errors.append(f'basis {filename} digest mismatch; review drift before a new revision')
        # The architecture reader tolerates damaged history for readback. A
        # contract validator must not silently skip a corrupt decision event.
        ledger_path = root / 'decisions/events.jsonl'
        if ledger_path.exists():
            for line in ledger_path.read_text(encoding='utf-8').splitlines():
                if line.strip() and not isinstance(json.loads(line), dict):
                    raise ValueError('decision ledger event must be an object')
        ledger = latest_decisions(root)
        for decision in data['decisions']:
            if decision['status'] == 'accepted':
                actual = ledger.get(decision['id'], {}).get('data', {})
                if actual.get('status') != 'accepted':
                    errors.append(f'accepted governing decision absent/not accepted: {decision["id"]}')
        profiles = project.get('release_profiles', {})
        for name in data['authority']['release']['profiles']:
            if name not in profiles:
                errors.append(f'unknown release profile: {name}')
        # Resolve deployment targets only through configured release profiles.
        targets = {p['deployment']['target'] for p in profiles.values()
                   if isinstance(p, dict) and isinstance(p.get('deployment'), dict)
                   and isinstance(p['deployment'].get('target'), str)}
        if set(data['authority']['deployment']['targets']) - targets:
            errors.append('deployment authority references an unconfigured target')
        for task_id in data['authority']['permitted_tasks']:
            paths = [root / 'tasks' / state / f'{task_id}.json'
                     for state in ('open', 'active', 'blocked', 'done')
                     if (root / 'tasks' / state / f'{task_id}.json').is_file()]
            if len(paths) != 1:
                errors.append(f'missing or duplicated task: {task_id}')
                continue
            task = read(paths[0])
            if task.get('id') != task_id or task.get('project') != data['project'] or task.get('status') != paths[0].parent.name:
                errors.append(f'task identity/state mismatch: {task_id}')
            raw_outcomes = task.get('requested_outcomes', [])
            requirements = {o['id']: o for o in raw_outcomes
                            if isinstance(o, dict) and isinstance(o.get('id'), str)}
            if len(requirements) != len(raw_outcomes):
                errors.append(f'{task_id}: malformed or duplicate task outcome IDs')
            contract = task.get('execution_contract')
            if data['authority']['mode'] == 'execute':
                errors.extend(f'{task_id}: {error}' for error in validate_execution_contract(contract))
                if isinstance(contract, dict):
                    for key in ('model', 'critic_model'):
                        if key in contract and contract[key] not in data['authority']['models']:
                            errors.append(f'{task_id}: {key} outside campaign model profiles')
                    release = contract.get('release')
                    if (isinstance(release, dict) and release.get('mode') == 'required'
                            and release.get('profile') not in data['authority']['release']['profiles']):
                        errors.append(f'{task_id}: release profile outside campaign authority')
            for outcome in data['goal']['outcomes']:
                for link in outcome['task_outcomes']:
                    if link['task_id'] != task_id:
                        continue
                    if link['requirement_id'] not in requirements:
                        errors.append(f'unknown task outcome: {task_id}:{link["requirement_id"]}')
                    else:
                        text = requirements[link['requirement_id']].get('text')
                        if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != link['text_sha256']:
                            errors.append(f'task outcome text drift: {task_id}:{link["requirement_id"]}')
                    if isinstance(contract, dict) and isinstance(contract.get('release'), dict):
                        release = contract['release']
                        if release.get('mode') == 'required' and 'release' not in outcome['required_evidence']:
                            errors.append(f'{task_id}: required release evidence missing from goal')
                        profile = profiles.get(release.get('profile'), {})
                        if isinstance(profile.get('deployment'), dict) and profile['deployment'].get('mode') == 'required' and 'live' not in outcome['required_evidence']:
                            errors.append(f'{task_id}: required live evidence missing from goal')
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(f'campaign references unavailable or invalid: {exc}')
    return errors


def snapshot_task_scope(repo: Path) -> dict[str, dict[str, Any]]:
    """Freeze all unfinished task identities and outcomes, including blocked work."""
    result = {}
    for state in ('open', 'active', 'blocked'):
        for path in sorted((repo / '.go/tasks' / state).glob('*.json')):
            task = json.loads(path.read_text(encoding='utf-8'))
            task_id = task['id']
            if task_id in result:
                raise ValueError(f'duplicate task state: {task_id}')
            outcomes = task.get('requested_outcomes', [])
            result[task_id] = {
                'status': state, 'summary': task['summary'],
                'outcomes': [{'id': item['id'], 'text_sha256': hashlib.sha256(item['text'].encode()).hexdigest()}
                             for item in outcomes],
            }
    return result


def limit_reached(value: float, limit: int | None) -> bool:
    """None is explicit absence of a campaign ceiling, never a numeric sentinel."""
    return limit is not None and value >= limit


def proven_progress(repo: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """Count delivery evidence, never Git activity or a mutable status label."""
    from .campaign_delivery import delivery_report, _task
    from .completion import completion_findings
    completed, remaining, findings = [], [], {}
    for task_id in contract['authority']['permitted_tasks']:
        try:
            report = delivery_report(repo, task_id)
            proof_errors = completion_findings(repo, _task(repo, task_id), current=False, remote=False)
            delivered = report['delivered'] and not proof_errors
            findings[task_id] = report['blockers'] + [{'code': 'invalid_content_proof', 'message': error} for error in proof_errors]
            if (contract.get('execution') or {}).get('shipping'):
                from .delivery_closure import inspect_closure
                closure = inspect_closure(repo, task_id, expected_policy=contract["execution"]["shipping"]["policy"])
                delivered = delivered and closure['delivered']
                findings[task_id] += [{'code': 'closure_pending', 'message': error} for error in closure['blockers']]
        except (ValueError, OSError, KeyError, TypeError) as exc:
            delivered = False
            findings[task_id] = [{'code': 'unproven', 'message': str(exc)}]
        (completed if delivered else remaining).append(task_id)
    return {'completed': completed, 'remaining': remaining, 'findings': findings,
            'total': len(completed) + len(remaining)}
