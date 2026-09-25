"""Evidence-bound repair admission and future-task amendments.

Canonical tasks, hierarchy and campaign contracts remain authoritative. The
journal is only a recoverable multi-file intent. Selection/claim must consult
pending_change_findings before work and respect the campaign-controller lock.
"""
from copy import deepcopy
from contextlib import nullcontext, ExitStack
import base64
from datetime import datetime, timezone
import fnmatch
import hashlib
from pathlib import Path

from .campaign_contracts import (contract_digest, validate_campaign_contract,
    campaign_task_findings, _revision_findings)
from .completion import contract_digest as task_digest
from .execution_contracts import dependency_findings
from .state_io import atomic_json, repository_lock
from .worktrees import read_object, registry_path, require_run_idle

SCHEMA = 'go-workflow.campaign-change.v1'


def _safe(repo, path):
    path = Path(path)
    if not path.is_absolute():
        path = repo / path
    if '..' in path.parts or not path.is_relative_to(repo):
        raise ValueError('Campaign change path outside repository')
    for node in (path, *path.parents):
        if node == repo:
            return path
        if node.is_symlink():
            raise ValueError('Campaign changes cannot traverse symlinks')
    raise ValueError('Campaign change path outside repository')


def _task(repo, identity):
    registry_path(repo, identity)
    paths = [_safe(repo, repo / '.go/tasks' / state / (identity + '.json'))
             for state in ('open', 'active', 'blocked', 'done')]
    found = [path for path in paths if path.exists()]
    if len(found) != 1:
        raise ValueError('Missing or duplicated task: ' + identity)
    task = read_object(found[0])
    if task.get('id') != identity or task.get('status') != found[0].parent.name:
        raise ValueError('Task identity/state mismatch')
    return found[0], task


def _inside(paths, bounds):
    return all(any(path == bound or (not any(mark in path for mark in '*?[')
                   and fnmatch.fnmatchcase(path, bound)) for bound in bounds) for path in paths)


def _proof(repo, evidence):
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('Exact source evidence is required')
    result = []
    for ref in evidence:
        if not isinstance(ref, dict) or set(ref) != {'path', 'sha256'}:
            raise ValueError('Evidence requires path and sha256')
        path = _safe(repo, ref['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']:
            raise ValueError('Source evidence changed')
        result.append(dict(ref))
    return result


def pending_change_findings(repo):
    repo = Path(repo).resolve()
    result = []
    for path in (repo / '.go/runs/campaigns').glob('*/changes/*.json'):
        value = read_object(_safe(repo, path))
        if value.get('schema') != SCHEMA or value.get('phase') != 'complete':
            result.append('Pending campaign change: ' + str(path.relative_to(repo)))
    return result


def _add_write(repo, writes, path, after):
    path = _safe(repo, path)
    writes[str(path.relative_to(repo))] = {
        'before': read_object(path) if path.exists() else None, 'after': after}


def _apply(repo, path, intent):
    if intent.get('schema') != SCHEMA:
        raise ValueError('Invalid campaign change journal')
    # Check ALL targets before any mutation, including recovery after lost ack.
    for name, entry in intent['writes'].items():
        target = _safe(repo, name)
        actual = read_object(target) if target.exists() else None
        if actual not in (entry['before'], entry['after']):
            raise ValueError('User changes conflict with campaign recovery: ' + name)
    for name, entry in intent['writes'].items():
        if entry['after'] is None:
            _safe(repo, name).unlink(missing_ok=True)
        else:
            atomic_json(_safe(repo, name), entry['after'])
    intent['phase'] = 'complete'
    atomic_json(path, intent)
    return {'campaign_path': intent['campaign_path'], 'previous_campaign': intent['previous_campaign'],
            'revision': intent['revision'], 'change_id': intent['change_id'], 'kind': intent['kind']}


def _validate_task(repo, proposed, candidates):
    from .cli import validate_task
    findings = validate_task(proposed, 'campaign change') + dependency_findings(repo, proposed, candidates=candidates)
    if findings:
        raise ValueError('Invalid task amendment: ' + '; '.join(findings))


def _revision_writes(repo, path, old, new, writes, change):
    errors = validate_campaign_contract(new) + _revision_findings(new, old)
    if errors:
        raise ValueError('Invalid campaign revision: ' + '; '.join(errors))
    directory = repo / '.go/runs/campaigns' / old['id']
    old_snapshot = directory / f"contract-r{old['revision']}-{contract_digest(old)[:12]}.json"
    new_snapshot = directory / f"contract-r{new['revision']}-{contract_digest(new)[:12]}.json"
    for snapshot, value in ((old_snapshot, old), (new_snapshot, new)):
        if snapshot.exists() and read_object(snapshot) != value:
            raise ValueError('Immutable campaign snapshot collision')
        _add_write(repo, writes, snapshot, value)
    _add_write(repo, writes, path, new)
    state_path = directory / 'state.json'
    if state_path.exists():
        from .campaign import _validate_state
        state = read_object(state_path); _validate_state(state)
        if state['dispatch'] is not None and state['dispatch'].get('stage') != 'returned':
            raise ValueError('Pause live task dispatch before changing campaign')
        if state['contract']['sha256'] != contract_digest(old):
            raise ValueError('Campaign state is not bound to prior revision')
        updated = deepcopy(state)
        updated['contract'] = {'revision': new['revision'], 'sha256': contract_digest(new),
                              'source_path': str(path), 'snapshot_path': str(new_snapshot.relative_to(repo))}
        updated['history'].append(change)
        _validate_state(updated)
        _add_write(repo, writes, state_path, updated)
    return str(old_snapshot.relative_to(repo))


def _change(repo, campaign_path, *, change_id, kind, request, owner, reason, evidence, build):
    repo = Path(repo).resolve(); path = _safe(repo, campaign_path)
    registry_path(repo, change_id)
    if not all(isinstance(value, str) and value.strip() for value in (owner, reason)):
        raise ValueError('Change owner and reason are required')
    with ExitStack() as locks:
        locks.enter_context(repository_lock(repo / '.go', 'campaign-controller'))
        identities = sorted({value for value in (request.get('parent_id'), request.get('task_id'), (request.get('repair_task') or {}).get('id')) if value})
        for identity in identities:
            registry_path(repo, identity)
            locks.enter_context(repository_lock(repo / '.go', 'managed-run-' + identity,timeout_seconds=0.1))
        for identity in identities:
            registry_path(repo, identity)
            locks.enter_context(repository_lock(repo / '.go', 'managed-state-' + identity))
        for identity in identities:
            locks.enter_context(repository_lock(repo / '.go', 'task-' + identity))
            require_run_idle({'control_repo': str(repo), 'task_id': identity})
        old = read_object(path)
        registry_path(repo, old['id'])
        journal = _safe(repo, repo / '.go/runs/campaigns' / old['id'] / 'changes' / (change_id + '.json'))
        request = {'kind': kind, 'request': request, 'owner': owner, 'reason': reason,
                   'evidence': evidence, 'campaign_path': str(path.relative_to(repo))}
        digest = contract_digest(request)
        if journal.exists():
            intent = read_object(journal)
            if intent.get('request_sha256') != digest:
                raise ValueError('Campaign change retry arguments differ')
            if intent.get('phase') == 'complete':
                # Later changes are allowed, but original revision must remain immutable.
                snapshot = repo / intent['new_snapshot']
                if contract_digest(read_object(snapshot)) != intent['new_sha256']:
                    raise ValueError('Completed revision snapshot changed')
                return {'campaign_path': intent['campaign_path'], 'previous_campaign': intent['previous_campaign'],
                        'revision': intent['revision'], 'change_id': change_id, 'kind': kind}
            return _apply(repo, journal, intent)
        if pending_change_findings(repo):
            raise ValueError('Recover pending campaign changes first')
        errors = validate_campaign_contract(old)
        if errors or old['authority']['mode'] != 'execute':
            raise ValueError('An executable valid campaign is required: ' + '; '.join(errors))
        from .campaign import load_contract
        old = load_contract(repo, path)
        evidence = _proof(repo, evidence)
        new = deepcopy(old); new['revision'] += 1; new['previous_sha256'] = contract_digest(old)
        writes = {}
        change = {'event': 'campaign.' + kind, 'created_at': datetime.now(timezone.utc).isoformat(), 'change_id': change_id, 'owner': owner,
                  'reason': reason, 'source_evidence': evidence, 'request_sha256': digest}
        build(repo, old, new, writes, change)
        previous = _revision_writes(repo, path, old, new, writes, change)
        snapshot = repo / '.go/runs/campaigns' / old['id'] / f"contract-r{new['revision']}-{contract_digest(new)[:12]}.json"
        intent = {'schema': SCHEMA, 'phase': 'prepared', 'change_id': change_id, 'kind': kind,
                  'request_sha256': digest, 'campaign_path': str(path.relative_to(repo)),
                  'previous_campaign': previous, 'revision': new['revision'],
                  'new_snapshot': str(snapshot.relative_to(repo)), 'new_sha256': contract_digest(new),
                  'change': change, 'writes': writes,
                  'source_snapshots': [{**ref, 'base64': base64.b64encode(_safe(repo, ref['path']).read_bytes()).decode()} for ref in evidence]}
        atomic_json(journal, intent)
        return _apply(repo, journal, intent)


def _link_hierarchy(repo, parent_id, repair_id):
    hierarchy = read_object(repo / '.go/hierarchy.json')
    def attach(node):
        if isinstance(node, dict):
            tasks = node.get('tasks')
            if isinstance(tasks, list) and parent_id in tasks:
                if repair_id not in tasks:
                    tasks.insert(tasks.index(parent_id), repair_id)
                return True
            return any(attach(value) for key, value in node.items() if key != 'tasks')
        if isinstance(node, list):
            return any(attach(item) for item in node)
        return False
    if not attach(hierarchy):
        raise ValueError('Original task is not in canonical hierarchy')
    return hierarchy


def admit_repair(repo, campaign_path, *, change_id, parent_id, repair_task,
                 outcome_ids, failure_fingerprint, owner, reason, evidence):
    """Admit one necessary repair; no recursive repair and no authority expansion."""
    request = {'parent_id': parent_id, 'repair_task': repair_task, 'outcome_ids': outcome_ids,
               'failure_fingerprint': failure_fingerprint}
    def build(repo, old, new, writes, change):
        _, parent = _task(repo, parent_id)
        if parent_id not in old['authority']['permitted_tasks'] or parent['status'] not in {'open', 'blocked'}:
            raise ValueError('Repair parent must be adopted and open/blocked; pause and block active work first')
        require_run_idle({'control_repo': str(repo), 'task_id': parent_id})
        if parent.get('campaign_repair'):
            raise ValueError('Recursive repair requires original-route reassessment')
        if parent.get('delivery_block'):
            raise ValueError('Reassess joint delivery before admitting external repair')
        allowance = old['authority']['expansion']['repair']
        if (not isinstance(outcome_ids, list) or not outcome_ids or len(set(outcome_ids)) != len(outcome_ids)
                or not set(outcome_ids) <= set(allowance['outcome_ids'])):
            raise ValueError('Repair lacks adopted outcome authority')
        outcomes = {item['id']: item for item in old['goal']['outcomes']}
        if any(not any(link['task_id'] == parent_id for link in outcomes[identity]['task_outcomes']) for identity in outcome_ids):
            raise ValueError('Repair outcome is unrelated to original task')
        if not isinstance(failure_fingerprint, str) or len(failure_fingerprint) != 64 or any(ch not in '0123456789abcdef' for ch in failure_fingerprint):
            raise ValueError('Exact failure fingerprint required')
        run = read_object(repo / '.go/runs/campaigns' / old['id'] / 'state.json')
        if not any(item.get('task_id') == parent_id and item.get('fingerprint') == failure_fingerprint for item in run.get('failures', [])):
            raise ValueError('Failure fingerprint is not recorded for the original task')
        admitted = 0
        for identity in old['authority']['permitted_tasks']:
            _, task = _task(repo, identity)
            if task.get('campaign_repair', {}).get('campaign_id') == old['id']:
                admitted += 1
        if admitted >= allowance['max_tasks']:
            raise ValueError('Necessary repair allowance exhausted; reassess route')
        task = deepcopy(repair_task); identity = task['id']; registry_path(repo, identity)
        if any((repo / '.go/tasks' / state / (identity + '.json')).exists() for state in ('open', 'active', 'blocked', 'done')):
            raise ValueError('Repair task already exists')
        if (task.get('project') != old['project'] or task.get('status') != 'open'
                or (task.get('claim') or {}).get('agent') or task.get('campaign_repair') or task.get('delivery_block')):
            raise ValueError('Repair must be a new unclaimed canonical task')
        if not _inside(task['scope']['modify'], allowance['modify']):
            raise ValueError('Repair scope exceeds delegated bounds')
        if any(edge.get('task_id') == parent_id and edge.get('project') == parent['project'] for edge in task.get('dependencies', [])):
            raise ValueError('Repair cannot wait for its blocked parent')
        new['authority']['permitted_tasks'].insert(0, identity)
        task['campaign_repair'] = {'schema': 'go-workflow.campaign-repair.v1', 'campaign_id': old['id'],
            'parent_id': parent_id, 'parent_contract_sha256': task_digest(parent), 'outcome_ids': outcome_ids,
            'failure_fingerprint': failure_fingerprint, 'reason': reason, 'change_id': change_id,
            'source_evidence': evidence}
        requirements = task.get('requested_outcomes', [])
        if not requirements:
            raise ValueError('Repair requires explicit outcomes')
        required = ['verification', 'critic']
        release = task['execution_contract']['release']
        if release['mode'] == 'required':
            required.append('release')
            project = read_object(repo / '.go/project.json')
            if (project.get('release_profiles', {}).get(release.get('profile'), {}).get('deployment') or {}).get('mode') == 'required':
                required.append('live')
        used = {item['id'] for item in new['goal']['outcomes']}; number = 1
        while 'O' + str(number) in used:
            number += 1
        new['goal']['outcomes'].append({'id': 'O' + str(number), 'text': 'Necessary repair for ' + parent_id + ': ' + reason,
            'task_outcomes': [{'task_id': identity, 'requirement_id': item['id'],
                              'text_sha256': hashlib.sha256(item['text'].encode()).hexdigest()} for item in requirements],
            'required_evidence': required})
        errors = campaign_task_findings(new, task)
        if errors:
            raise ValueError('Repair exceeds execution authority: ' + '; '.join(errors))
        _validate_task(repo, task, [task])
        _add_write(repo, writes, repo / '.go/tasks/open' / (identity + '.json'), task)
        _add_write(repo, writes, repo / '.go/hierarchy.json', _link_hierarchy(repo, parent_id, identity))
        change.update(task_id=identity, parent_id=parent_id, failure_fingerprint=failure_fingerprint,
                      parent_contract_sha256=task_digest(parent), outcome_ids=outcome_ids)
    return _change(repo, campaign_path, change_id=change_id, kind='repair_admitted', request=request,
                   owner=owner, reason=reason, evidence=evidence, build=build)


def amend_future_task(repo, campaign_path, *, change_id, task_id, patch, owner, reason, evidence):
    """Change implementation detail of open work without weakening original outcomes."""
    def build(repo, old, new, writes, change):
        path, task = _task(repo, task_id)
        if task_id not in old['authority']['permitted_tasks'] or task['status'] != 'open' or task.get('delivery_block'):
            raise ValueError('Only adopted, open, ungrouped future tasks may be amended')
        require_run_idle({'control_repo': str(repo), 'task_id': task_id})
        if (task.get('claim') or {}).get('agent') or (repo / '.go/runs' / task_id / 'run-state.json').exists():
            raise ValueError('Future task already has an execution checkpoint; reconcile it explicitly')
        allowed = {'summary', 'description', 'acceptance', 'verification', 'scope', 'dependencies', 'context_files', 'skill_files'}
        if not isinstance(patch, dict) or not patch or set(patch) - allowed:
            raise ValueError('Unsupported future-task amendment')
        updated = deepcopy(task); updated.update(deepcopy(patch))
        for key in ('acceptance', 'verification'):
            if not set(task.get(key, [])) <= set(updated.get(key, [])):
                raise ValueError('Original acceptance and verification cannot be removed')
        if not _inside(updated['scope']['modify'], task['scope']['modify']):
            raise ValueError('Future-task amendment cannot enlarge modify authority')
        _validate_task(repo, updated, [updated])
        updated.setdefault('campaign_amendments', []).append({**change,
            'before_contract_sha256': task_digest(task), 'after_contract_sha256': task_digest(updated),
            'changed_fields': sorted(patch)})
        _add_write(repo, writes, path, updated)
        change.update(task_id=task_id, changed_fields=sorted(patch),
                      before_contract_sha256=task_digest(task), after_contract_sha256=task_digest(updated))
    return _change(repo, campaign_path, change_id=change_id, kind='task_amended',
                   request={'task_id': task_id, 'patch': patch}, owner=owner, reason=reason, evidence=evidence, build=build)



def _parent_base_handoff(repo, parent_id, state):
    from .worktrees import git_text, validate_record
    record = validate_record(read_object(registry_path(repo, parent_id)))
    workspace = state['workspace']
    if (record['task_id'] != parent_id or record['run_id'] != state['run_id']
            or record['base_branch'] != workspace['base_branch']
            or record['base_commit'] != workspace['base_commit']):
        raise ValueError('Parent workspace/checkpoint binding requires reconciliation')
    current = git_text(repo, 'rev-parse', '--verify', 'refs/heads/' + record['base_branch'])
    if current != record['base_commit']:
        return {'resumed': False, 'task_id': parent_id,
                'reason': 'workspace_reconciliation_required',
                'old_base': record['base_commit'], 'current_base': current,
                'next_action': 'Reconcile the owned parent workspace with the current base, preserve dirty work, and refresh dependent checkpoint evidence before resuming.'}
    return None

def resume_repaired_parent(repo, repair_id, actor, controller_locked=False, reconcile=False):
    """Resume only the unchanged original task after proven repair delivery."""
    from .completion import completion_findings
    from .campaign_delivery import delivery_report
    from .run_state import read_state, require_stopped, protected_task
    from .execution_context import json_hash
    repo = Path(repo).resolve()
    with ExitStack() as locks:
        if not controller_locked:
            locks.enter_context(repository_lock(repo / '.go', 'campaign-controller'))
        _, repair = _task(repo, repair_id)
        link = repair.get('campaign_repair')
        if not link:
            return {'resumed': False, 'reason': 'not_a_repair'}
        parent_id = link['parent_id']
        for identity in sorted({parent_id, repair_id}):
            locks.enter_context(repository_lock(repo / '.go', 'managed-run-' + identity,timeout_seconds=0.1))
        for identity in sorted({parent_id, repair_id}):
            locks.enter_context(repository_lock(repo / '.go', 'managed-state-' + identity))
        for identity in sorted({parent_id, repair_id}):
            locks.enter_context(repository_lock(repo / '.go', 'task-' + identity))
            require_run_idle({'control_repo': str(repo), 'task_id': identity})
        _, repair = _task(repo, repair_id)
        if repair.get('campaign_repair') != link:
            raise ValueError('Repair lineage changed during parent resume')
        if repair['status'] != 'done' or completion_findings(repo, repair, current=False, remote=False) or not delivery_report(repo, repair_id)['delivered']:
            raise ValueError('Original task cannot resume before verified repair delivery')
        directory = repo / '.go/runs/campaigns' / link['campaign_id'] / 'changes'
        journal = _safe(repo, directory / ('resume-' + repair_id + '.json'))
        if journal.exists():
            intent = read_object(journal)
            if intent.get('actor') != actor or intent.get('parent_id') != parent_id or intent.get('repair_id') != repair_id:
                raise ValueError('Parent resume identity changed')
            if intent.get('phase') != 'complete':
                if (repo / '.go/runs' / parent_id / 'run-state.json').exists():
                    state = read_state(repo, parent_id); require_stopped(state)
                    if reconcile:
                        from .repair_reconciliation import reconcile_repair_parent
                        try:
                            recovered = reconcile_repair_parent(repo, parent_id, repair_id, actor, locks_held=True)
                        except ValueError as exc:
                            return {'resumed': False, 'task_id': parent_id, 'reason': 'repair_reconciliation_blocked', 'next_action': str(exc)}
                        if not recovered['reconciled']:
                            return recovered
                        state = read_state(repo, parent_id)
                    handoff = _parent_base_handoff(repo, parent_id, state)
                    if handoff:
                        return handoff
                _apply(repo, journal, intent)
                return {'resumed': True, 'task_id': parent_id, 'status': _task(repo, parent_id)[1]['status']}
        source, parent = _task(repo, parent_id)
        if task_digest(parent) != link['parent_contract_sha256']:
            raise ValueError('Original task contract changed after repair admission')
        if parent['status'] in {'open', 'active', 'done'}:
            return {'resumed': False, 'reason': 'parent_not_blocked', 'task_id': parent_id}
        require_run_idle({'control_repo': str(repo), 'task_id': parent_id})
        if any((repo / '.go/tasks/active').glob('*.json')):
            raise ValueError('Another task is active; defer parent resumption')
        proposed = deepcopy(parent); proposed.pop('blocked', None)
        state_path = repo / '.go/runs' / parent_id / 'run-state.json'
        if state_path.exists():
            state = read_state(repo, parent_id); require_stopped(state)
            if state['owner'] != actor or (parent.get('claim') or {}).get('agent') != actor:
                raise ValueError('Parent claim requires explicit ownership handoff')
            proposed['status'] = 'active'
            if json_hash(protected_task(proposed)) != state['task_hash']:
                raise ValueError('Parent checkpoint contract requires explicit reconciliation')
            if reconcile:
                from .repair_reconciliation import reconcile_repair_parent
                try:
                    recovered = reconcile_repair_parent(repo, parent_id, repair_id, actor, locks_held=True)
                except ValueError as exc:
                    return {'resumed': False, 'task_id': parent_id, 'reason': 'repair_reconciliation_blocked', 'next_action': str(exc)}
                if not recovered['reconciled']:
                    return recovered
                state = read_state(repo, parent_id)
            handoff = _parent_base_handoff(repo, parent_id, state)
            if handoff:
                return handoff
        else:
            if (parent.get('claim') or {}).get('agent'):
                raise ValueError('Blocked claimed task has no resumable checkpoint')
            proposed['status'] = 'open'
        directory = repo / '.go/runs/campaigns' / link['campaign_id'] / 'changes'
        journal = _safe(repo, directory / ('resume-' + repair_id + '.json'))
        if journal.exists():
            raise ValueError('Parent was blocked again after successful repair; reassess new failure')
        else:
            writes = {}
            _add_write(repo, writes, repo / '.go/tasks' / proposed['status'] / (parent_id + '.json'), proposed)
            _add_write(repo, writes, source, None)
            intent = {'schema': SCHEMA, 'phase': 'prepared', 'change_id': 'resume-' + repair_id,
                      'kind': 'parent_resumed', 'writes': writes, 'campaign_path': None,
                      'previous_campaign': None, 'revision': None, 'parent_id': parent_id,
                      'repair_id': repair_id, 'actor': actor}
            atomic_json(journal, intent)
        _apply(repo, journal, intent)
        return {'resumed': True, 'task_id': parent_id, 'status': proposed['status']}
