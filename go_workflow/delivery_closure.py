"""Synchronize existing completion records without repeating product publication.

The committed manifest binds authoritative task/run/evidence bytes. A local Git
journal only recovers effects; portable delivery truth comes from committed bytes
and remote readback, not that journal. No task status is invented here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time

from .state_io import atomic_json, repository_lock
from .worktrees import git, git_text, read_object, registry_path

SCHEMA = 'go-workflow.delivery-closure.v1'


def _path(repo, task_id):
    registry_path(repo, task_id)
    path=repo / '.go/runs' / task_id / 'delivery-closure.json'
    for parent in (path, *path.parents):
        if parent==repo:break
        if parent.is_symlink():raise ValueError('Closure path contains a symbolic link')
    return path


def _sha(path):
    if path.is_symlink():
        raise ValueError('Closure cannot follow a symbolic link: ' + str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _git(repo, *args, env=None):
    from .release import _environment
    result=subprocess.run(['git', '-c', 'protocol.ext.allow=never', *args], cwd=repo, text=True, capture_output=True, env=env if env is not None else _environment(), timeout=120)
    if result.returncode:
        raise ValueError('Closure Git command failed: ' + result.stderr.strip())
    return result.stdout.strip()


def _effect(repo, operation, *args, env=None):
    """Small effect seam for lost-ack tests; intent is durable before invocation."""
    return _git(repo, *args, env=env)


def _remote(repo, remote, branch):
    url=_git(repo,'remote','get-url',remote)
    output=_git(repo,'ls-remote','--heads',remote,'refs/heads/'+branch)
    lines=output.splitlines()
    if len(lines)!=1 or lines[0].split()[1]!='refs/heads/'+branch:
        raise ValueError('Configured closure branch is missing or ambiguous on remote')
    return url,lines[0].split()[0]


def _ancestor(repo, ancestor, descendant):
    return git(repo,'merge-base','--is-ancestor',ancestor,descendant,check=False).returncode==0


def _verify_files(repo, manifest):
    required={'schema','task_id','project','run_id','policy','remote','branch','remote_url','product_commit','files'}
    if set(manifest)!=required or manifest.get('schema')!=SCHEMA:
        raise ValueError('Invalid closure manifest shape')
    task_id=manifest['task_id']
    _path(repo,task_id)
    if not isinstance(manifest['files'],dict):
        raise ValueError('Invalid closure files')
    expected={f'.go/tasks/{status}/{task_id}.json' for status in ('open','active','blocked','done')}
    expected.add(str(registry_path(repo,task_id).relative_to(repo)))
    coordinator=read_object(repo/'.go/tasks/done'/f'{task_id}.json')
    for member in (coordinator.get('delivery_block') or {}).get('member_ids',[]):
        registry_path(repo,member)
        expected.update(f'.go/tasks/{status}/{member}.json' for status in ('open','active','blocked','done'))
    if not expected.issubset(manifest['files']):
        raise ValueError('Closure lacks canonical task/workspace records')
    if not all(isinstance(manifest[key],str) and manifest[key] for key in ('project','run_id','branch')):
        raise ValueError('Invalid closure identity fields')
    _git(repo,'check-ref-format','refs/heads/'+manifest['branch'])
    if not re.fullmatch('[0-9a-f]{40}',str(manifest['product_commit'])):
        raise ValueError('Invalid closure product commit')
    if manifest['policy']=='push' and (not isinstance(manifest['remote'],str)
            or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*',manifest['remote'])
            or not isinstance(manifest['remote_url'],str) or not manifest['remote_url']):
        raise ValueError('Invalid closure remote binding')
    if manifest['policy'] not in {'push','local-commit'}:
        raise ValueError('Invalid closure policy')
    for name, digest in manifest['files'].items():
        path=Path(name)
        if (path.is_absolute() or '..' in path.parts
                or (name not in expected and not name.startswith(f'.go/runs/{task_id}/'))):
            raise ValueError('Closure path escaped workflow records')
        for parent in (repo/path, *(repo/path).parents):
            if parent==repo:break
            if parent.is_symlink():raise ValueError('Closure proof path contains a symbolic link')
        if digest is not None and (not isinstance(digest,str) or len(digest)!=64):
            raise ValueError('Invalid closure hash')
        if _sha(repo/path)!=digest:
            raise ValueError('Closure record changed: '+name)



def _validate_completed_records(repo, manifest):
    from .completion import completion_findings,content_snapshot,read_artifact
    from .worktrees import validate_record
    task_id=manifest['task_id']
    task=read_object(repo/'.go/tasks/done'/f'{task_id}.json')
    if (task.get('id')!=task_id or task.get('project')!=manifest['project']
            or task.get('status')!='done' or task.get('work_status')!='completed'
            or task.get('review_status')!='approved' or 'execution_contract' not in task):
        raise ValueError('Closure task is not approved completed work')
    if any((repo/f'.go/tasks/{status}/{task_id}.json').exists() for status in ('open','active','blocked')):
        raise ValueError('Closure task has conflicting states')
    record=validate_record(read_object(registry_path(repo,task_id)))
    integrated=(record.get('integration') or {}).get('integrated_commit')
    released=(task.get('release_receipt') or {}).get('commit')
    if manifest['product_commit'] != (released or integrated):
        raise ValueError('Closure product identity differs from verified integration/release')
    if record['state']!='cleaned' or record['run_id']!=manifest['run_id']:
        raise ValueError('Closure workspace is not cleaned for this run')
    for path in (repo/'.go/runs'/task_id).rglob('*'):
        if path.is_file() and path.name!='delivery-closure.json' and str(path.relative_to(repo)) not in manifest['files']:
            raise ValueError('Closure omitted a task proof record')
    errors=completion_findings(repo,task,current=False,remote=True)
    if errors:
        raise ValueError('Closure content evidence invalid: '+str(errors))
    verification=read_artifact(repo/'.go',task['completion_evidence']['verification'])
    if content_snapshot(repo,task,manifest['product_commit'])['digest']!=verification['content_digest']:
        raise ValueError('Closure product bytes differ from verified content')
    run_path=repo/'.go/runs'/task_id/'run-state.json'
    release_path=repo/'.go/runs'/task_id/'release-state.json'
    if run_path.is_file():
        from .run_state import validate_state
        run=read_object(run_path);validate_state(run)
        binding=(run.get('publication') or {}).get('taskwise_delivery')
        if binding and (binding['ship_policy']!=manifest['policy'] or binding['branch']!=manifest['branch']
                        or binding['remote_url']!=manifest['remote_url']):
            raise ValueError('Closure differs from frozen managed delivery authority')
        if run['phase']!='complete' or run['task_id']!=task_id or run['run_id']!=manifest['run_id']:
            raise ValueError('Closure managed run incomplete or mismatched')
    elif release_path.is_file():
        from .release import validate_state
        release=validate_state(read_object(release_path))
        if release['phase']!='published' or release['task_id']!=task_id or release['run_id']!=manifest['run_id']:
            raise ValueError('Closure publisher is incomplete or mismatched')
    else:
        raise ValueError('Closure has no authoritative terminal run')


def inspect_closure(repo, task_id, *, expected_policy=None):
    """Portable readback; never invokes publisher, model, or deployment."""
    repo=Path(repo).resolve()
    try:
        task_path=repo/'.go/tasks/done'/f'{task_id}.json'
        if task_path.exists():
            task=read_object(task_path)
            if (task.get('delivery_block') or {}).get('role')=='member':
                from .delivery_blocks import member_completion_findings
                coordinator=task['delivery_block']['coordinator_id']
                if coordinator==task_id:raise ValueError('Self-referential delivery member')
                errors=member_completion_findings(repo,task)
                if errors:raise ValueError('; '.join(errors))
                return inspect_closure(repo,coordinator,expected_policy=expected_policy)
        path=_path(repo,task_id)
        manifest=read_object(path)
        if manifest.get('schema')!=SCHEMA or manifest.get('task_id')!=task_id:
            raise ValueError('Invalid closure identity')
        if expected_policy is not None and manifest.get('policy')!=expected_policy:
            raise ValueError('Closure policy differs from campaign authority')
        _verify_files(repo,manifest)
        _validate_completed_records(repo,manifest)
        relative=str(path.relative_to(repo))
        commit=_git(repo,'log','-1','--format=%H','--',relative)
        if not commit or _git(repo,'show',commit+':'+relative)!=path.read_text().strip():
            raise ValueError('Closure manifest is not committed')
        if not _ancestor(repo,manifest['product_commit'],commit):
            raise ValueError('Closure does not preserve the verified product commit')
        for name,digest in manifest['files'].items():
            result=subprocess.run(['git','show',commit+':'+name],cwd=repo,capture_output=True)
            actual=hashlib.sha256(result.stdout).hexdigest() if result.returncode==0 else None
            if actual!=digest:
                raise ValueError('Closure commit does not contain exact record: '+name)
        policy=manifest['policy']
        push='not_authorized'
        if policy=='push':
            url,head=_remote(repo,manifest['remote'],manifest['branch'])
            if url!=manifest['remote_url']:
                raise ValueError('Closure remote URL changed')
            if head!=commit:
                _git(repo,'fetch','--no-tags',manifest['remote'],'refs/heads/'+manifest['branch'])
                if not _ancestor(repo,commit,head):
                    raise ValueError('Remote does not preserve closure commit')
            push='verified'
        elif policy!='local-commit':
            raise ValueError('Invalid committed closure policy')
        return {'delivered':True,'commit':commit,'push':push,'blockers':[]}
    except (ValueError,OSError,KeyError,TypeError,subprocess.TimeoutExpired) as exc:
        return {'delivered':False,'commit':None,'push':'unconfirmed','blockers':[str(exc)]}


def _manifest(repo,task_id,policy):
    from .campaign_delivery import delivery_report
    from .completion import completion_findings
    task=read_object(repo/'.go/tasks/done'/f'{task_id}.json')
    report=delivery_report(repo,task_id)
    errors=completion_findings(repo,task,current=False,remote=True)
    if not report['delivered'] or errors:
        raise ValueError('Closure requires verified completed work: '+str(report['blockers']+errors))
    record=read_object(registry_path(repo,task_id))
    release=(task.get('execution_contract') or {}).get('release',{})
    project=read_object(repo/'.go/project.json')
    profile=project.get('release_profiles',{}).get(release.get('profile'),{})
    run_path=repo/'.go/runs'/task_id/'run-state.json'
    binding=(read_object(run_path).get('publication') or {}).get('taskwise_delivery') if run_path.exists() else None
    if binding and binding['ship_policy']!=policy:
        raise ValueError('Closure authority differs from frozen managed run')
    remote=binding['remote'] if binding else profile.get('remote','origin')
    branch=binding['branch'] if binding else profile.get('branch',record['base_branch'])
    if _git(repo,'symbolic-ref','--short','HEAD')!=branch:
        raise ValueError('Closure must synchronize the configured base branch')
    url,remote_head=_remote(repo,remote,branch) if policy=='push' else (None,None)
    if binding and policy=='push' and url!=binding['remote_url']:
        raise ValueError('Closure remote differs from frozen managed authority')
    head=_git(repo,'rev-parse','HEAD')
    if remote_head and not _ancestor(repo,remote_head,head):
        raise ValueError('Remote advanced; reconcile before closure')
    names={f'.go/tasks/{state}/{task_id}.json' for state in ('open','active','blocked','done')}
    names.add(str(registry_path(repo,task_id).relative_to(repo)))
    for member in (task.get('delivery_block') or {}).get('member_ids',[]):
        registry_path(repo,member)
        names.update(f'.go/tasks/{status}/{member}.json' for status in ('open','active','blocked','done'))
    directory=repo/'.go/runs'/task_id
    for path in directory.rglob('*'):
        if path.is_file() and path.name!='delivery-closure.json':
            names.add(str(path.relative_to(repo)))
    # Completion manifests and their raw proof live under the task run directory.
    return {'schema':SCHEMA,'task_id':task_id,'project':task['project'],
            'run_id':record['run_id'],'policy':policy,'remote':remote,'branch':branch,
            'remote_url':url,'product_commit':(task.get('release_receipt') or {}).get('commit') or record['integration']['integrated_commit'],
            'files':{name:_sha(repo/name) for name in sorted(names)}}



def _check_recovery_index(repo, journal, paths):
    for name in paths:
        staged=_git(repo,'ls-files','--stage','--',name).splitlines()
        if len(staged)>1:
            raise ValueError('Conflicted closure index; preserve user changes')
        current=None
        if staged:
            parts=staged[0].split()
            if parts[2]!='0':raise ValueError('Conflicted closure index')
            current=(parts[0],parts[1])
        allowed=[]
        for tree in (journal['parent'],journal['tree']):
            entry=_git(repo,'ls-tree',tree,'--',name).split()
            allowed.append((entry[0],entry[2]) if entry else None)
        if current not in allowed:
            raise ValueError('User staged closure changes during recovery; preserve them')

def synchronize_closure(repo,task_id,*,policy):
    repo=Path(repo).resolve()
    if policy not in {'none','local-commit','push'}:
        raise ValueError('Invalid closure authority')
    if policy=='none':
        return {'delivered':False,'commit':None,'push':'not_authorized','blockers':['Commit prohibited by run authority']}
    with repository_lock(repo/'.go','workspace-integration'):
        path=_path(repo,task_id)
        if path.exists():
            manifest=read_object(path)
            if manifest.get('policy')!=policy:
                raise ValueError('Existing closure authority cannot be enlarged silently')
            result=inspect_closure(repo,task_id)
            if result['delivered']:
                return result
            _verify_files(repo,manifest)
            _validate_completed_records(repo,manifest)
        else:
            manifest=_manifest(repo,task_id,policy)
            _verify_files(repo,manifest)
            _validate_completed_records(repo,manifest)
            atomic_json(path,manifest)
        if manifest.get('task_id')!=task_id:
            raise ValueError('Closure task identity changed')
        if _git(repo,'symbolic-ref','--short','HEAD')!=manifest['branch']:
            raise ValueError('Closure branch changed; preserve pending effects')
        from .worktrees import require_run_idle
        require_run_idle(read_object(registry_path(repo,task_id)))
        paths=sorted(set(manifest['files'])|{str(path.relative_to(repo))})
        gitdir=Path(_git(repo,'rev-parse','--absolute-git-dir'))
        journal_path=gitdir/'go-delivery-closure'/f'{task_id}.json'
        if journal_path.exists():
            journal=read_object(journal_path)
            if (set(journal)!={'parent','tree','timestamp','manifest_sha256','branch'}
                    or journal['branch']!=manifest['branch']
                    or not all(re.fullmatch('[0-9a-f]{40}',str(journal[key])) for key in ('parent','tree'))):
                raise ValueError('Invalid closure recovery journal')
            if journal['manifest_sha256']!=_sha(path):
                raise ValueError('Closure candidate changed after commit intent')
        else:
            # Refuse pre-existing staged changes on owned paths; other staged files remain untouched.
            staged=set(_git(repo,'diff','--cached','--name-only').splitlines())
            if staged.intersection(paths):
                raise ValueError('Closure-owned paths have staged changes; reconcile explicitly')
            parent=_git(repo,'rev-parse','HEAD')
            descriptor,index=tempfile.mkstemp(prefix='go-closure-index-',dir=gitdir)
            os.close(descriptor);os.unlink(index)
            from .release import _environment
            env={**_environment(),'GIT_INDEX_FILE':index}
            try:
                _git(repo,'read-tree',parent,env=env)
                existing=[name for name in paths if (repo/name).exists()]
                missing=[name for name in paths if not (repo/name).exists()]
                if existing:_git(repo,'add','--',*existing,env=env)
                if missing:_git(repo,'update-index','--force-remove','--',*missing,env=env)
                tree=_git(repo,'write-tree',env=env)
            finally:
                if Path(index).exists():Path(index).unlink()
            journal={'parent':parent,'tree':tree,'timestamp':str(int(time.time()))+' +0000',
                     'manifest_sha256':_sha(path),'branch':manifest['branch']}
            atomic_json(journal_path,journal)
        changed=set(_git(repo,'diff','--name-only',journal['parent'],journal['tree']).splitlines())
        if not changed.issubset(set(paths)):
            raise ValueError('Closure candidate contains unrelated changes')
        for name in paths:
            raw=subprocess.run(['git','show',journal['tree']+':'+name],cwd=repo,capture_output=True)
            candidate_hash=hashlib.sha256(raw.stdout).hexdigest() if raw.returncode==0 else None
            if candidate_hash!=_sha(repo/name):
                raise ValueError('Closure candidate tree differs from approved records')
        _check_recovery_index(repo,journal,paths)
        from .release import _environment
        env={**_environment(),'GIT_AUTHOR_DATE':journal['timestamp'],'GIT_COMMITTER_DATE':journal['timestamp']}
        commit=_effect(repo,'commit','commit-tree',journal['tree'],'-p',journal['parent'],
                       '-m','Synchronize delivery '+task_id,env=env)
        head=_git(repo,'rev-parse','HEAD')
        if head==journal['parent']:
            _effect(repo,'advance','update-ref','refs/heads/'+journal['branch'],commit,head)
        elif head!=commit:
            raise ValueError('Local branch advanced during closure; reconcile preserved candidate')
        # Align only our index entries; preserve unrelated staged/unstaged data.
        _git(repo,'reset','-q',commit,'--',*paths)
        if policy=='push':
            url,remote_head=_remote(repo,manifest['remote'],manifest['branch'])
            if url!=manifest['remote_url']:
                raise ValueError('Closure remote URL changed')
            if remote_head!=commit:
                if not _ancestor(repo,remote_head,journal['parent']):
                    raise ValueError('Remote advanced during closure; candidate preserved')
                _effect(repo,'push','push',manifest['remote'],commit+':refs/heads/'+manifest['branch'])
        result=inspect_closure(repo,task_id)
        if not result['delivered']:
            raise ValueError('Closure readback failed: '+str(result['blockers']))
        return result
