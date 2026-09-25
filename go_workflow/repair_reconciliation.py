"""Reconcile one blocked parent against an exactly delivered repair.

No reset, stash or publication. Candidate objects are computed before changing
workspace refs; conflicts preserve the original dirty checkout. One journal
recovers the checkpoint/merge and canonical registry/run updates.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import tempfile

from .state_io import atomic_json, repository_lock
from .worktrees import (active_task, checked_scope, git, git_text, owned_record,
                        read_object, registry_path, require_run_idle,
                        require_visible_index, validate_record, verify_workspace)

SCHEMA = 'go-workflow.repair-reconciliation.v1'


def pending_reconciliation_findings(repo):
    repo=Path(repo).resolve()
    findings=[]
    for path in (repo/'.go/runs').glob('*/repair-reconciliation-*.json'):
        record=read_object(_safe(repo,path))
        if record.get('schema')!=SCHEMA or record.get('phase')!='complete':
            findings.append('Pending parent workspace reconciliation: '+str(path.relative_to(repo)))
    return findings


def _effect(operation, action, *args, **kwargs):
    return action(*args, **kwargs)


def _safe(repo, path):
    if not path.is_relative_to(repo):
        raise ValueError('Repair journal path outside repository')
    for part in (path, *path.parents):
        if part == repo:
            return path
        if part.is_symlink():
            raise ValueError('Repair journal cannot follow symlinks')
    raise ValueError('Repair journal path outside repository')


def _candidate_tree(worker, before):
    fd, index = tempfile.mkstemp(prefix='go-repair-index-')
    os.close(fd); os.unlink(index)
    env = dict(os.environ)
    for key in ('GIT_DIR','GIT_COMMON_DIR','GIT_WORK_TREE','GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES','GIT_NAMESPACE'):
        env.pop(key, None)
    env['GIT_INDEX_FILE'] = index
    def run(*args):
        result = subprocess.run(['git','-C',str(worker),*args],env=env,text=True,capture_output=True)
        if result.returncode:
            raise ValueError(result.stderr.strip())
        return result.stdout.strip()
    try:
        run('read-tree', before)
        run('add', '--all', '--', '.')
        return run('write-tree')
    finally:
        Path(index).unlink(missing_ok=True)


def _commit_object(worker, tree, parents, message):
    args = ['-c','user.name=go-workflow','-c','user.email=go-workflow@local.invalid',
            'commit-tree', tree]
    for parent in parents:
        args.extend(['-p', parent])
    return git_text(worker, *args, '-m', message)


def _base_and_remote(repo, state, record, repair_id):
    from .delivery_closure import inspect_closure
    from .release import _remote
    closure = inspect_closure(repo, repair_id)
    if not closure['delivered']:
        raise ValueError('Repair closure is not verified')
    current = git_text(repo, 'rev-parse', 'refs/heads/' + record['base_branch'])
    if (git_text(repo, 'symbolic-ref', '--short', 'HEAD') != record['base_branch']
            or closure['commit'] != current):
        raise ValueError('Current base is not the exact delivered repair closure; reassess external drift')
    publication = state.get('publication', {})
    if publication.get('ship_policy') not in {'commit','push'}:
        raise ValueError('Frozen parent authority prohibits checkpoint commits')
    policy = publication.get('taskwise_delivery')
    if policy and policy['ship_policy'] == 'none':
        raise ValueError('Frozen parent authority prohibits checkpoint commits')
    if policy and policy['ship_policy'] == 'push':
        url, refs = _remote(repo, {'provider':'git-tag','remote':policy['remote']})
        if (not publication.get('allow_push') or url != policy['remote_url']
                or policy['branch'] != record['base_branch']
                or refs.get('refs/heads/' + policy['branch']) != current
                or closure['push'] != 'verified'):
            raise ValueError('Repair does not match frozen remote identity and exact branch readback')
    return current


def reconcile_repair_parent(repo, parent_id, repair_id, actor, *, locks_held=False):
    """Return a truthful handoff on conflict; never activate the blocked parent."""
    from .completion import contract_digest
    from .run_state import read_state, require_stopped, run_code, validate_state
    repo = Path(repo).resolve()
    registry_path(repo, parent_id); registry_path(repo, repair_id)
    journal_path = _safe(repo, repo / '.go/runs' / parent_id / ('repair-reconciliation-' + repair_id + '.json'))
    with ExitStack() as locks:
        if not locks_held:
            locks.enter_context(repository_lock(repo / '.go','campaign-controller'))
            for identity in sorted({parent_id,repair_id}):
                locks.enter_context(repository_lock(repo / '.go','managed-run-' + identity,timeout_seconds=0.1))
            for identity in sorted({parent_id,repair_id}):
                locks.enter_context(repository_lock(repo / '.go','managed-state-' + identity))
            for identity in sorted({parent_id,repair_id}):
                locks.enter_context(repository_lock(repo / '.go','task-' + identity))
        locks.enter_context(repository_lock(repo / '.go','workspace-integration'))
        locks.enter_context(repository_lock(repo / '.go','workspace-execution-' + parent_id))
        parent = active_task(repo,parent_id)
        if parent['status'] != 'blocked' or (parent.get('claim') or {}).get('agent') != actor:
            raise ValueError('Reconciliation requires the owned blocked parent')
        repair = active_task(repo,repair_id)
        link = repair.get('campaign_repair') or {}
        if link.get('parent_id') != parent_id or link.get('parent_contract_sha256') != contract_digest(parent):
            raise ValueError('Delivered repair does not bind unchanged parent contract')
        state = read_state(repo,parent_id); require_stopped(state)
        record = verify_workspace(owned_record(repo,parent_id,actor,state['run_id'],active=False))
        require_run_idle(record)
        if record['state'] != 'ready' or state['owner'] != actor:
            raise ValueError('Parent workspace is not owned and ready')
        if state['effects'] or (repo / '.go/runs' / parent_id / 'release-state.json').exists():
            raise ValueError('Parent has publication effects; resume exact readback instead')
        worker = Path(record['path'])
        require_visible_index(record)
        if git_text(worker,'ls-files','--others','--ignored','--exclude-standard'):
            raise ValueError('Parent has ignored workspace files; preserve them before reconciliation')
        if git_text(worker,'ls-files','-u'):
            raise ValueError('Parent has unresolved index conflicts')
        current = _base_and_remote(repo,state,record,repair_id)
        if journal_path.exists():
            intent = read_object(journal_path)
            if (intent.get('schema') != SCHEMA or intent.get('parent_id') != parent_id
                    or intent.get('repair_id') != repair_id or intent.get('actor') != actor
                    or intent.get('new_base') != current or intent.get('parent_contract') != contract_digest(parent)):
                raise ValueError('Repair reconciliation binding changed')
        else:
            if record['base_commit'] == current:
                return {'reconciled':True,'task_id':parent_id,'old_base':current,'current_base':current}
            if record['base_commit'] != state['workspace']['base_commit']:
                raise ValueError('Parent registry/checkpoint requires explicit reconciliation')
            if git(repo,'merge-base','--is-ancestor',record['base_commit'],current,check=False).returncode:
                raise ValueError('Repair base does not preserve original parent history')
            if git_text(worker,'diff','--cached','--name-only'):
                raise ValueError('Parent has staged changes; preserve and review them before reconciliation')
            checked_scope(record)
            before = git_text(worker,'rev-parse','HEAD')
            tree = _candidate_tree(worker,before)
            checkpoint = _commit_object(worker,tree,[before],'Checkpoint blocked parent ' + parent_id)
            merge = git(worker,'merge-tree','--write-tree',checkpoint,current,check=False)
            if merge.returncode:
                return {'reconciled':False,'resumed':False,'task_id':parent_id,
                        'reason':'repair_merge_conflict','old_base':record['base_commit'],'current_base':current,
                        'conflict_details':merge.stdout.strip(),
                        'next_action':'Resolve the reported source conflict within parent scope; dirty workspace bytes are unchanged.'}
            merged = _commit_object(worker,merge.stdout.splitlines()[0],[checkpoint,current],
                                    'Reconcile delivered repair ' + repair_id + ' into ' + parent_id)
            intent = {'schema':SCHEMA,'phase':'prepared','parent_id':parent_id,'repair_id':repair_id,
                      'actor':actor,'parent_contract':contract_digest(parent),'old_base':record['base_commit'],
                      'new_base':current,'before':before,'checkpoint':checkpoint,'merged':merged,
                      'checkpoint_tree':tree,'old_record':record,'old_state':state,
                      'created_at':datetime.now(timezone.utc).isoformat()}
            atomic_json(journal_path,intent)
        for actual, old, new in ((record,intent['old_record'],intent.get('new_record')),
                                  (state,intent['old_state'],intent.get('new_state'))):
            if actual != old and actual != new:
                raise ValueError('Parent checkpoint changed during reconciliation; preserve user state')
        head = git_text(worker,'rev-parse','HEAD')
        if head in {intent['before'],intent['checkpoint']}:
            if _candidate_tree(worker,intent['before']) != intent['checkpoint_tree']:
                raise ValueError('Parent working bytes changed after checkpoint intent')
            index_tree = git_text(worker,'write-tree')
            allowed_index = {git_text(worker,'rev-parse',intent['before']+'^{tree}'),intent['checkpoint_tree']}
            if index_tree not in allowed_index:
                raise ValueError('Parent staged bytes changed after checkpoint intent')
            if head == intent['before']:
                _effect('commit',git,worker,'update-ref','refs/heads/'+record['branch'],intent['checkpoint'],head)
            # All dirty files were proved in-scope and index unchanged before intent.
            _effect('index',git,worker,'read-tree',intent['checkpoint'])
            if git_text(worker,'status','--porcelain','--untracked-files=all'):
                raise ValueError('Parent checkpoint readback is not clean')
            _effect('merge',git,worker,'-c','core.hooksPath=/dev/null','merge','--ff-only',intent['merged'])
            head = git_text(worker,'rev-parse','HEAD')
        if head != intent['merged'] or git_text(worker,'status','--porcelain','--untracked-files=all'):
            raise ValueError('Parent reconciliation readback changed; preserve workspace')
        if 'new_record' not in intent:
            updated = deepcopy(intent['old_record']);updated['base_commit'] = current
            updated.setdefault('reconciliations',[]).append({
                'old_base_commit':intent['old_base'],'new_base_commit':current,
                'workspace_head_before':intent['before'],'workspace_head_after':intent['merged'],
                'reconciled_at':intent['created_at'],'evidence_invalidated':True})
            validate_record(updated);checked_scope(updated)
            refreshed = deepcopy(intent['old_state'])
            refreshed['history'].append({'event':'repair.base_reconciled','repair_id':repair_id,
                'journal':str(journal_path.relative_to(repo)),
                'old_base':intent['old_base'],'new_base':current,
                'invalidated_checks':refreshed['checks'],'invalidated_phase_evidence':refreshed['phase_evidence']})
            refreshed.update(workspace=updated,phase='verify',check_index=0,checks=[],phase_evidence=[],
                             inflight=None,worker_group=None,code=run_code(worker,updated,parent))
            policy = refreshed.get('publication',{}).get('taskwise_delivery')
            if policy and policy['ship_policy'] == 'push':
                policy['remote_base'] = current
            validate_state(refreshed)
            intent.update(new_record=updated,new_state=refreshed)
            atomic_json(journal_path,intent)
        # Re-read exact target bytes before overwrite; lost acknowledgements accept only our saved after-state.
        for path, old, new, operation in (
                (registry_path(repo,parent_id),intent['old_record'],intent['new_record'],'registry'),
                (repo/'.go/runs'/parent_id/'run-state.json',intent['old_state'],intent['new_state'],'checkpoint')):
            if read_object(path) not in (old,new):
                raise ValueError('Parent metadata changed during recovery')
            _effect(operation,atomic_json,path,new)
        intent['phase']='complete';atomic_json(journal_path,intent)
        return {'reconciled':True,'task_id':parent_id,'old_base':intent['old_base'],'current_base':current,
                'checkpoint_commit':intent['checkpoint'],'merge_commit':intent['merged']}
