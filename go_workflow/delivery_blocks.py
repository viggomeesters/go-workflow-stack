"""Pre-claim joint delivery over canonical tasks, with recoverable intent.

Call pending_block_findings before selection/claim. All writers must respect the
campaign-controller lock. This module never declares a member complete.
"""
from copy import deepcopy
from contextlib import ExitStack
from pathlib import Path
import hashlib

from .completion import contract_digest, completion_findings
from .execution_contracts import validate_execution_contract
from .state_io import atomic_json, repository_lock
from .worktrees import read_object, registry_path

SCHEMA = 'go-workflow.delivery-block.v1'
INTENT = 'go-workflow.delivery-block-intent.v1'


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _safe(repo, path):
    for item in (path, *path.parents):
        if item == repo:
            return path
        if item.is_symlink():
            raise ValueError('Delivery block cannot traverse symlinks')
    raise ValueError('Delivery block path outside repository')


def _locate(repo, identity):
    registry_path(repo, identity)
    paths = [_safe(repo, repo / '.go/tasks' / state / (identity + '.json'))
             for state in ('open', 'active', 'blocked', 'done')]
    existing = [path for path in paths if path.exists()]
    if len(existing) != 1:
        raise ValueError('Missing or duplicate block task: ' + identity)
    task = read_object(existing[0])
    if task.get('id') != identity or task.get('status') != existing[0].parent.name:
        raise ValueError('Block task identity/state mismatch')
    return existing[0], task


def pending_block_findings(repo):
    repo = Path(repo).resolve()
    findings = []
    for path in (repo / '.go/runs').glob('*/delivery-block-intent.json'):
        intent = read_object(_safe(repo, path))
        if intent.get('schema') != INTENT or intent.get('phase') != 'complete':
            findings.append('Joint delivery preparation pending: ' + path.parent.name)
    for path in (repo / '.go/runs').glob('*/delivery-block-finalization.json'):
        intent=read_object(_safe(repo,path))
        if intent.get('schema')!='go-workflow.delivery-block-finalization.v1' or intent.get('phase')!='complete':
            findings.append('Joint delivery finalization pending: '+path.parent.name)
    return findings


def _check_cycles(tasks):
    visiting, visited = set(), set()
    def visit(identity):
        if identity in visiting:
            raise ValueError('Delivery block introduces or contains a dependency cycle')
        if identity in visited:
            return
        visiting.add(identity)
        task = tasks[identity]
        for edge in task.get('dependencies', []):
            if edge['project'] == task['project'] and edge['task_id'] in tasks:
                visit(edge['task_id'])
        visiting.remove(identity); visited.add(identity)
    for identity in tasks:
        visit(identity)


def _record_paths(repo, identity):
    return [_safe(repo, repo / '.go/tasks' / state / (identity + '.json'))
            for state in ('open', 'active', 'blocked', 'done')]


def _apply_task(repo, identity, value):
    target = _safe(repo, repo / '.go/tasks' / value['status'] / (identity + '.json'))
    # Intent blocks selectors throughout. Write target first; recovery accepts
    # only journaled duplicate bytes and completes removal after a crash.
    atomic_json(target, value)
    for source in _record_paths(repo, identity):
        if source != target:
            source.unlink(missing_ok=True)


def _new_intent(repo, coordinator, members, reason, owner):
    identities = [coordinator] + members
    originals = {identity: _locate(repo, identity)[1] for identity in identities}
    for task in originals.values():
        if (repo/'.go/runs'/task['id']/'run-state.json').exists():
            raise ValueError('Joint delivery requires tasks without an existing managed run')
        if task['status'] != 'open' or task.get('delivery_block') or (task.get('claim') or {}).get('agent'):
            raise ValueError('Joint delivery requires unclaimed open tasks')
        if not task.get('requested_outcomes') or not task.get('execution_contract'):
            raise ValueError('Joint delivery requires original outcomes and execution contracts')
        if validate_execution_contract(task['execution_contract']):
            raise ValueError('Invalid block execution contract')
        workspace = task['execution_contract'].get('workspace', {})
        if workspace.get('mode') != 'task_worktree' or workspace.get('control_state') != 'repo_local_single_writer':
            raise ValueError('Joint delivery requires a managed task workspace')
    first = originals[coordinator]
    for task in originals.values():
        if task['project'] != first['project']:
            raise ValueError('Joint delivery requires the same project')
        for key in set(first['execution_contract']) | set(task['execution_contract']):
            if task['execution_contract'].get(key) != first['execution_contract'].get(key):
                raise ValueError('Incompatible joint delivery ' + key)
        for key in ('execution_mode','shareable_delivery','repository_context','notepad_path'):
            if task.get(key)!=first.get(key):raise ValueError('Incompatible joint delivery '+key)
        if task.get('architecture') != first.get('architecture'):
            raise ValueError('Joint delivery architecture must be reconciled before grouping')
    all_tasks = {}
    for state in ('open', 'active', 'blocked', 'done'):
        for path in (repo / '.go/tasks' / state).glob('*.json'):
            task = read_object(_safe(repo, path))
            if task['id'] in all_tasks:
                raise ValueError('Duplicate canonical task')
            all_tasks[task['id']] = task
    _check_cycles(all_tasks)
    proposed = deepcopy(originals)
    combined = proposed[coordinator]
    combined['description'] = 'Joint delivery: ' + reason + '\n\n' + '\n\n'.join(
        identity + ' — ' + task['summary'] + '\n' + task.get('description', '')
        for identity, task in originals.items())
    mappings = []
    seen = {item['id'] for item in combined['requested_outcomes']}
    if len(seen) != len(combined['requested_outcomes']):
        raise ValueError('Duplicate coordinator outcome IDs')
    next_id = 1
    for identity, task in originals.items():
        if len({item['id'] for item in task['requested_outcomes']}) != len(task['requested_outcomes']):
            raise ValueError('Duplicate original outcome IDs')
        for item in task['requested_outcomes']:
            target_id = item['id']
            if identity != coordinator:
                while 'R' + str(next_id) in seen:
                    next_id += 1
                target_id = 'R' + str(next_id); seen.add(target_id)
                combined['requested_outcomes'].append({'id': target_id, 'text': item['text'],
                    'source': 'delivery_block:' + identity + ':' + item['id'],
                    'status': 'pending', 'evidence': []})
            mappings.append({'task_id': identity, 'requirement_id': item['id'],
                             'coordinator_requirement_id': target_id, 'text_sha256': _digest(item['text'])})
    combined['description']='\n\n'.join(identity+': '+task['summary']+'\n'+task.get('description','') for identity,task in originals.items())
    for key in ('acceptance', 'verification'):
        values = [value for task in originals.values() for value in task.get(key, [])]
        if values:
            combined[key] = list(dict.fromkeys(values))
    for key in ('context_files','skill_files'):
        values=[task.get(key,[]) for task in originals.values()]
        if any(isinstance(value,dict) for value in values):
            phases={'build','verify','critic','repair','release_prepare','release','setup','cleanup'}
            for value in values:
                if isinstance(value,dict):phases.update(value)
            merged={}
            for phase in sorted(phases):
                paths=[]
                for value in values:
                    selected=value.get(phase,[]) if isinstance(value,dict) else value
                    if not isinstance(selected,list) or any(not isinstance(item,str) for item in selected):
                        raise ValueError('Invalid phase-specific '+key)
                    paths.extend(selected)
                merged[phase]=list(dict.fromkeys(paths))
            combined[key]=merged
        else:
            if any(not isinstance(value,list) for value in values):raise ValueError('Invalid '+key)
            paths=list(dict.fromkeys(item for value in values for item in value))
            if paths:combined[key]=paths
    combined['scope'] = {key: list(dict.fromkeys(value for task in originals.values()
                         for value in task['scope'].get(key, [])))
                         for key in set().union(*(task['scope'] for task in originals.values()))}
    dependencies = []
    for task in originals.values():
        for edge in task.get('dependencies', []):
            if edge['project'] == first['project'] and edge['task_id'] in identities:
                continue
            if edge not in dependencies:
                dependencies.append(edge)
    combined['dependencies'] = dependencies
    for identity in members:
        proposed[identity]['status'] = 'blocked'
        proposed[identity]['dependencies'] = [{'project': first['project'], 'task_id': coordinator,
                                               'requires': 'done_with_required_release_evidence'
                                               if first['execution_contract']['release']['mode'] == 'required' else 'done'}]
    all_tasks.update(proposed); _check_cycles(all_tasks)
    original_hashes = {identity: contract_digest(task) for identity, task in originals.items()}
    prepared_hashes = {identity: contract_digest(task) for identity, task in proposed.items()}
    metadata = {'schema': SCHEMA, 'coordinator_id': coordinator, 'member_ids': members,
                'reason': reason, 'owner': owner, 'originals': originals,
                'original_contracts': original_hashes, 'prepared_contracts': prepared_hashes,
                'outcome_map': mappings}
    combined['delivery_block'] = metadata
    for identity in members:
        proposed[identity]['delivery_block'] = {'schema': SCHEMA, 'coordinator_id': coordinator,
            'role': 'member', 'state': 'awaiting_joint_delivery',
            'original_contract': original_hashes[identity], 'prepared_contract': prepared_hashes[identity]}
    staged = deepcopy(first); staged['status'] = 'blocked'
    staged['delivery_block'] = {'schema': SCHEMA, 'coordinator_id': coordinator, 'state': 'preparing'}
    return {'schema': INTENT, 'phase': 'prepared', 'coordinator_id': coordinator,
            'member_ids': members, 'reason': reason, 'owner': owner,
            'originals': originals, 'proposed': proposed, 'staged_coordinator': staged}


def prepare_block(repo, coordinator_id, member_ids, reason, owner):
    """Freeze and group existing open tasks; retry exactly the same call to recover."""
    repo = Path(repo).resolve()
    registry_path(repo, coordinator_id)
    if (not isinstance(member_ids, list) or not member_ids or coordinator_id in member_ids
            or len(member_ids) != len(set(member_ids))):
        raise ValueError('Provide distinct non-coordinator member IDs')
    for identity in member_ids:
        registry_path(repo, identity)
    if any(not isinstance(value, str) or not value.strip() for value in (reason, owner)):
        raise ValueError('Joint delivery requires a reason and owner')
    journal = _safe(repo, repo / '.go/runs' / coordinator_id / 'delivery-block-intent.json')
    with repository_lock(repo / '.go', 'campaign-controller'), ExitStack() as locks:
        for identity in sorted([coordinator_id]+member_ids):
            locks.enter_context(repository_lock(repo/'.go','managed-run-'+identity,timeout_seconds=0.1))
        for identity in sorted([coordinator_id]+member_ids):
            locks.enter_context(repository_lock(repo/'.go','task-'+identity,timeout_seconds=0.1))
        if journal.exists():
            intent = read_object(journal)
            if any(intent.get(key) != value for key, value in [('schema', INTENT),
                    ('coordinator_id', coordinator_id), ('member_ids', member_ids), ('reason', reason), ('owner', owner)]):
                raise ValueError('Delivery block recovery intent changed')
            if intent['phase'] == 'complete':
                errors = membership_findings(repo, coordinator_id)
                if errors:
                    raise ValueError('; '.join(errors))
                return _locate(repo, coordinator_id)[1]['delivery_block']
        else:
            if pending_block_findings(repo):
                raise ValueError('Recover the pending delivery block before preparing another')
            intent = _new_intent(repo, coordinator_id, member_ids, reason, owner)
            atomic_json(journal, intent)
        for identity in [coordinator_id] + member_ids:
            records = [read_object(path) for path in _record_paths(repo, identity) if path.exists()]
            allowed = [intent['originals'][identity], intent['proposed'][identity]]
            if identity == coordinator_id:
                allowed.append(intent['staged_coordinator'])
            if not records or any(actual not in allowed for actual in records):
                raise ValueError('Canonical task changed during block preparation: ' + identity)
        _apply_task(repo, coordinator_id, intent['staged_coordinator'])
        for identity in member_ids:
            _apply_task(repo, identity, intent['proposed'][identity])
        _apply_task(repo, coordinator_id, intent['proposed'][coordinator_id])
        intent['phase'] = 'complete'; atomic_json(journal, intent)
        return intent['proposed'][coordinator_id]['delivery_block']


def membership_findings(repo, coordinator_id):
    """Validate immutable originals and source mapping against canonical tasks."""
    repo = Path(repo).resolve()
    try:
        _, coordinator = _locate(repo, coordinator_id)
        meta = coordinator['delivery_block']
        if meta['schema'] != SCHEMA or meta['coordinator_id'] != coordinator_id:
            raise ValueError('Invalid delivery block identity')
        identities = [coordinator_id] + meta['member_ids']
        for identity in identities:
            original = meta['originals'][identity]
            if contract_digest(original) != meta['original_contracts'][identity]:
                raise ValueError('Frozen original contract changed: ' + identity)
            _, actual = _locate(repo, identity)
            if contract_digest(actual) != meta['prepared_contracts'][identity]:
                raise ValueError('Prepared block contract changed: ' + identity)
            if actual['delivery_block'].get('coordinator_id') != coordinator_id:
                raise ValueError('Block membership changed: ' + identity)
        combined = {item['id']: item for item in coordinator['requested_outcomes']}
        expected = {(identity, item['id']) for identity in identities
                    for item in meta['originals'][identity]['requested_outcomes']}
        found, targets = set(), set()
        for mapping in meta['outcome_map']:
            pair = (mapping['task_id'], mapping['requirement_id'])
            target = mapping['coordinator_requirement_id']
            if pair in found or target in targets:
                raise ValueError('Duplicate block outcome mapping')
            found.add(pair); targets.add(target)
            original = next(item for item in meta['originals'][pair[0]]['requested_outcomes'] if item['id'] == pair[1])
            if _digest(original['text']) != mapping['text_sha256'] or _digest(combined[target]['text']) != mapping['text_sha256']:
                raise ValueError('Block source outcome drift')
        if expected != found or set(combined) != targets:
            raise ValueError('Block outcome coverage changed')
        return []
    except (ValueError, KeyError, TypeError, OSError, StopIteration) as exc:
        return [str(exc)]


def shared_proof_findings(repo, coordinator_id):
    """Precondition for finalization; never fabricates per-member completion proof."""
    errors = membership_findings(repo, coordinator_id)
    if errors:
        return errors
    _, task = _locate(Path(repo).resolve(), coordinator_id)
    if task['status'] != 'done' or task.get('review_status') != 'approved':
        return ['Joint coordinator is not approved and delivered']
    from .campaign_delivery import delivery_report
    report = delivery_report(Path(repo).resolve(), coordinator_id)
    return completion_findings(Path(repo).resolve(), task, current=False, remote=False) + [
        item['message'] for item in report['blockers']]


def finalize_block(repo, coordinator_id):
    """Close members only from the jointly verified coordinator's real evidence."""
    repo=Path(repo).resolve()
    _,coordinator=_locate(repo,coordinator_id)
    meta=coordinator.get('delivery_block') or {}
    if meta.get('coordinator_id')!=coordinator_id or 'member_ids' not in meta:
        return []
    with repository_lock(repo/'.go','delivery-block-finalize-'+coordinator_id), ExitStack() as locks:
        for identity in sorted([coordinator_id]+meta['member_ids']):
            locks.enter_context(repository_lock(repo/'.go','task-'+identity,timeout_seconds=0.1))
        _,coordinator=_locate(repo,coordinator_id)
        current_meta=coordinator.get('delivery_block') or {}
        if (current_meta.get('coordinator_id')!=coordinator_id
                or current_meta.get('member_ids')!=meta['member_ids']):
            raise ValueError('Joint membership changed before finalization; retry with current membership')
        meta=current_meta
        journal=repo/'.go/runs'/coordinator_id/'delivery-block-finalization.json'
        if not journal.exists():
            errors=shared_proof_findings(repo,coordinator_id)
            if errors:raise ValueError('Joint delivery proof incomplete: '+'; '.join(errors))
        else:
            errors=completion_findings(repo,coordinator,current=False,remote=False)
            workspace=read_object(registry_path(repo,coordinator_id))
            if errors or workspace.get('state')!='cleaned':
                raise ValueError('Joint coordinator proof changed during finalization')
        if journal.exists():
            intent=read_object(journal)
        else:
            proposed={}
            originals={}
            for identity in meta['member_ids']:
                _,member=_locate(repo,identity)
                if member['status']!='blocked':raise ValueError('Joint member left its waiting state')
                originals[identity]=deepcopy(member)
                member.update(status='done',work_status='completed',review_status='approved')
                member['delivery_block']['state']='delivered'
                member['completion_evidence']=deepcopy(coordinator['completion_evidence'])
                member['claim']=deepcopy(coordinator['claim'])
                for outcome in member['requested_outcomes']:
                    mapping=next(item for item in meta['outcome_map'] if item['task_id']==identity and item['requirement_id']==outcome['id'])
                    source=next(item for item in coordinator['requested_outcomes'] if item['id']==mapping['coordinator_requirement_id'])
                    outcome.update(status='verified',evidence=deepcopy(source['evidence']))
                if 'release_receipt' in coordinator:
                    member['release_receipt']={**deepcopy(coordinator['release_receipt']),'task_id':identity,
                        'shared_delivery_coordinator':coordinator_id}
                member.setdefault('review_history',[]).append({'status':'approved','agent':meta['owner'],
                    'evidence':'Shared verified candidate; original contract and outcomes bound by delivery_block metadata'})
                proposed[identity]=member
            intent={'schema':'go-workflow.delivery-block-finalization.v1','phase':'pending',
                    'coordinator_id':coordinator_id,'originals':originals,'proposed':proposed}
            atomic_json(journal,intent)
        if (intent.get('schema')!='go-workflow.delivery-block-finalization.v1'
                or intent.get('coordinator_id')!=coordinator_id
                or set(intent['proposed'])!=set(meta['member_ids'])):
            raise ValueError('Joint finalization intent mismatch')
        for identity in meta['member_ids']:
            records=[read_object(path) for path in _record_paths(repo,identity) if path.exists()]
            if not records or any(record not in (intent['originals'][identity],intent['proposed'][identity]) for record in records):
                raise ValueError('Joint member changed during finalization; preserve it')
        for identity,member in intent['proposed'].items():
            _apply_task(repo,identity,member)
        intent['phase']='complete';atomic_json(journal,intent)
        return list(meta['member_ids'])


def member_completion_findings(repo,task):
    """Validate shared execution against each unchanged original member contract."""
    try:
        meta=task['delivery_block'];coordinator_id=meta['coordinator_id']
        errors=shared_proof_findings(repo,coordinator_id)
        if errors:return errors
        journal=read_object(Path(repo)/'.go/runs'/coordinator_id/'delivery-block-finalization.json')
        if journal.get('phase')!='complete' or task!=journal['proposed'][task['id']]:
            return ['Joint member lacks exact completed finalization intent']
        _,coordinator=_locate(Path(repo),coordinator_id)
        combined={item['id']:item for item in coordinator['requested_outcomes']}
        mapped=[item for item in coordinator['delivery_block']['outcome_map'] if item['task_id']==task['id']]
        for item in mapped:
            if combined[item['coordinator_requirement_id']]['status']!='verified':
                return ['Joint member original requirement is not verified in shared candidate']
        return []
    except (ValueError,OSError,KeyError,TypeError) as exc:
        return [str(exc)]
