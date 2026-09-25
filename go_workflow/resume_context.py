"""Read current canonical state for resumption; conversation text is not input."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .completion import content_snapshot, contract_digest, read_artifact
from .execution_context import json_hash
from .worktrees import workflow_root, registry_path, read_object, git_text

SCHEMA='go-workflow.resume-context.v1'


def compose_resume_context(repo,task_id,*,actor=None):
    root=workflow_root(Path(repo).resolve());control=root.parent
    registry_path(control,task_id)
    records={}
    def read(path):
        if not path.resolve().is_relative_to(control) or any(node.is_symlink() for node in (path,*path.parents) if node!=control and node.is_relative_to(control)):
            raise ValueError('Canonical resume records cannot traverse symlinks')
        raw=path.read_bytes()
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('Canonical resume record must be an object')
        records[str(path.relative_to(control))]=hashlib.sha256(raw).hexdigest()
        return value
    paths=[root/'tasks'/status/(task_id+'.json') for status in ('open','active','blocked','done')]
    found=[path for path in paths if path.is_file()]
    if len(found)!=1:raise ValueError('Resume requires one authoritative task record')
    task=read(found[0])
    if task.get('id')!=task_id or task.get('status')!=found[0].parent.name:
        raise ValueError('Resume task identity/state mismatch')
    blockers=[];ownership_blocked=False
    report=None
    if task['status']=='done':
        from .campaign_delivery import delivery_report
        report=delivery_report(control,task_id)
        historical=root/'runs'/task_id/'run-state.json'
        closed=read(historical) if historical.is_file() else {}
        policy=closed.get('publication',{})
        if policy.get('taskwise_delivery') or policy.get('taskwise_candidate'):
            from .delivery_closure import inspect_closure
            closure=inspect_closure(control,task_id)
            if not closure['delivered']:
                report['delivered']=False
                report['blockers'] += [{'message':item} for item in closure['blockers']]
        if report['delivered']:
            for name,digest in records.items():
                if hashlib.sha256((control/name).read_bytes()).hexdigest()!=digest:
                    raise ValueError('Canonical completed state changed while reading; retry')
            return {'schema':SCHEMA,'task_id':task_id,'task_status':'done','run_id':closed.get('run_id'),'phase':'complete',
                    'owner':(task.get('claim') or {}).get('agent'),'completed':True,'next_action':'none',
                    'summary':f'{task_id} done — verified delivery already exists; no execution to repeat.',
                    'delivery':report,'source_records':records,'blockers':[]}
        blockers.extend(item['message'] for item in report['blockers'])
    run_path=root/'runs'/task_id/'run-state.json'
    state=read(run_path) if run_path.is_file() else None
    registry=registry_path(control,task_id)
    record=read(registry) if registry.is_file() else None
    if state:
        from .run_state import validate_state
        validate_state(state)
    if record:
        from .worktrees import validate_record
        validate_record(record)
    phase=state['phase'] if state else 'unclaimed'
    owner=state['owner'] if state else (task.get('claim') or {}).get('agent')
    if actor is not None and owner is not None and owner!=actor:
        blockers.append('Run owner differs; explicit ownership handoff required')
        ownership_blocked=True
    current_code=None;source_digest=None;source_revision=None;dependencies_match=None
    if state:
        from .run_state import validate_state,require_stopped,protected_task,run_code
        try:
            validate_state(state)
            try:require_stopped(state)
            except ValueError:
                ownership_blocked=True
                raise
            if state['control_repo']!=str(control):blockers.append('Active run belongs to another checkout; reconcile its workspace')
            if phase not in {'setup','release','cleanup','complete'} and json_hash(protected_task(task))!=state['task_hash']:
                blockers.append('Task contract changed; checkpoint migration required')
        except ValueError as exc:blockers.append(str(exc))
    workspace=Path(record['path']) if record else control
    trusted_workspace=True
    if record:
        try:
            from .worktrees import verify_workspace
            if record['control_repo']!=str(control):raise ValueError('Workspace belongs to another control checkout')
            verify_workspace(record)
        except ValueError as exc:
            blockers.append(str(exc));trusted_workspace=False
    if record and state and any(record.get(key)!=state['workspace'].get(key) for key in ('path','branch','base_branch','base_commit','run_id','owner','generation')):
        blockers.append('Workspace and checkpoint bindings differ')
    if trusted_workspace and workspace.is_dir():
        try:
            source_digest=content_snapshot(workspace,task)['digest']
            source_revision=git_text(workspace,'rev-parse','HEAD')
            if record and state:
                current_code=run_code(workspace,record,task)
                if state.get('proof_identity'):
                    from .proof_dependencies import proof_identity
                    dependencies_match=proof_identity(workspace,record,task,state['models'])==state['proof_identity']
                if current_code.get('control_base_head')!=record['base_commit']:
                    blockers.append('Workspace base is behind current control base; reconcile before execution')
        except ValueError as exc:blockers.append(str(exc))
    elif state and phase!='complete':blockers.append('Owned workspace is unavailable; preserve checkpoint and recover it')
    evidence={}
    from .completion import completion_findings
    full_findings=completion_findings(workspace,task,phase_only=True) if trusted_workspace and workspace.is_dir() else ['Workspace unavailable']
    for kind,ref in (task.get('completion_evidence') or {}).items():
        if kind=='schema':continue
        findings=[]
        try:
            artifact=read_artifact(root,ref)
            if (artifact.get('task_id')!=task_id or artifact.get('contract_digest')!=contract_digest(task)
                    or artifact.get('status')!='passed'):
                findings.append('Proof belongs to another contract or did not pass')
            if artifact.get('content_digest')!=source_digest:findings.append('Proof source bytes differ')
            if kind=='verification':
                from .proof_dependencies import valid_checks
                checks=artifact.get('checks')
                if (artifact.get('schema')!='go-workflow.executed-verification.v1'
                        or not trusted_workspace
                        or artifact.get('content_unchanged') is not True
                        or not isinstance(checks,list) or len(checks)!=len(task['verification'])
                        or not checks or not valid_checks(workspace,task,[{'returncode':0,'completion_check':check} for check in checks])):
                    findings.append('Verification lacks current executed command receipts')
            if kind=='critic' and current_code and (dependencies_match is False or (dependencies_match is None
                    and state['code'].get('contract_context_sha256')!=current_code.get('contract_context_sha256'))):
                findings.append('Critic context changed')
            records[ref['path']]=ref['sha256']
        except (ValueError,OSError,KeyError) as exc:findings.append(str(exc))
        phase_findings=findings if kind=='verification' else findings or full_findings
        evidence[kind]={'valid':not phase_findings,'source_matches':not findings,
                        'findings':phase_findings,'ref':ref}
    publication={}
    if state:
        publication={'authority':state.get('publication',{}),'effects':state.get('effects',{}),
                     'requires_readback':bool(state.get('effects'))}
    publication_path=root/'runs'/task_id/'release-state.json'
    if publication_path.is_file():
        published=read(publication_path)
        publication.update(phase=published.get('phase'),effects=published.get('effects',{}),requires_readback=True)
    if task['status']=='blocked':blockers.append((task.get('blocked') or {}).get('reason','Task is blocked'))
    if task['status']=='done' and report and not report['delivered']:
        next_action='recover_delivery'
        if ownership_blocked:next_action='resolve_blocker'
    elif blockers:next_action='resolve_blocker'
    elif state and state.get('inflight'):
        next_action='readback_publication' if phase=='release' else 'reconcile_interrupted_phase'
    elif not state:next_action='claim' if task['status']=='open' else 'reconcile_missing_checkpoint'
    elif publication.get('requires_readback') and phase=='release':next_action='readback_publication'
    elif current_code and current_code!=state['code'] and dependencies_match is not True:
        next_action={'build':'rebuild_current_source','repair':'repair_current_source',
                     'release_prepare':'prepare_current_candidate'}.get(phase,'verify_current_source')
    else:next_action=phase
    # Detect concurrent canonical writes; callers retry instead of using a mixed view.
    for name,digest in records.items():
        path=control/name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            blockers.append('Canonical state changed while composing resume context; retry')
            next_action='retry_snapshot';break
    return {'schema':SCHEMA,'task_id':task_id,'task_status':task['status'],
            'run_id':state.get('run_id') if state else None,'phase':phase,'owner':owner,'completed':False,
            'workspace':record,'source_revision':source_revision,'checkpoint_revision':(state or {}).get('code',{}).get('head'),
            'source_digest':source_digest,'evidence':evidence,'publication':publication,'delivery':report,
            'next_action':next_action,'blockers':blockers,'source_records':records,
            'summary':f'{task_id} {task["status"]} — phase {phase}; next: {next_action}.',
            'context_policy':'Current canonical records and exact source bytes govern; old chat is background only.'}
