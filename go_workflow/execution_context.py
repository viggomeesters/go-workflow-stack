"""Immutable managed-worker context and drift checks at the process boundary."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .state_io import atomic_json, atomic_write_text
from .worktrees import active_task, registered_workspace, git, git_text, changed_paths, workflow_root

SCHEMA = 'go-workflow.execution-context-snapshot.v1'
MAX_FILE_BYTES = 16 * 1024 * 1024


class ContextError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_hash(value: Any) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())


def file_state(path: Path, *, bounded=True) -> dict:
    if not path.exists() and not path.is_symlink(): return {'kind': 'missing'}
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        content = os.readlink(path).encode()
        kind = 'symlink'
    elif stat.S_ISREG(info.st_mode):
        if bounded and info.st_size > MAX_FILE_BYTES: raise ContextError('Context file exceeds explicit 16 MiB limit: ' + str(path))
        hasher = hashlib.sha256()
        size = 0
        with path.open('rb') as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
                size += len(chunk)
        return {'kind': 'file', 'mode': stat.S_IMODE(info.st_mode), 'size': size, 'sha256': hasher.hexdigest()}
    else:
        raise ContextError('Unsupported context file type; inspect before dispatch: ' + str(path))
    return {'kind': kind, 'mode': stat.S_IMODE(info.st_mode), 'size': len(content), 'sha256': digest(content)}


def git_state(workspace: Path, record: dict) -> dict:
    paths = changed_paths(record)
    tracked = sorted(set(name for name in git(workspace, 'ls-files', '-z').stdout.split('\0') if name))
    tracked_state = {name: file_state(workspace / name, bounded=False) for name in tracked}
    return {'head': git_text(workspace, 'rev-parse', 'HEAD'), 'base_commit': record['base_commit'],
            'branch': git_text(workspace, 'symbolic-ref', '--quiet', '--short', 'HEAD'),
            'index_sha256': digest(git(workspace, 'ls-files', '--stage', '-z').stdout.encode()),
            'tracked_content_sha256': json_hash(tracked_state),
            'files': {name: file_state(workspace / name) for name in paths}}


def _copy_candidate_path(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink(): shutil.rmtree(target)
        else: target.unlink()
    if not source.exists() and not source.is_symlink(): return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink(): target.symlink_to(os.readlink(source))
    else: shutil.copy2(source, target)


@contextmanager
def verification_checkout(workspace: Path, record: dict, candidate_code: dict):
    """Yield an unregistered checkout with the exact candidate file bytes."""
    workspace = workspace.resolve()
    candidate = git_state(workspace, record)
    for key in ('head', 'base_commit', 'branch', 'index_sha256', 'tracked_content_sha256', 'files'):
        if candidate_code.get(key) != candidate[key]:
            raise ContextError('Managed candidate changed before verification isolation')
    with tempfile.TemporaryDirectory(prefix='go-managed-verify-') as directory:
        checkout = Path(directory) / 'candidate'
        git(workspace, 'clone', '--quiet', '--no-hardlinks', '--no-checkout', '.', str(checkout))
        git(checkout, 'checkout', '--quiet', '-b', 'verification-' + uuid.uuid4().hex, candidate['head'])
        for name in candidate['files']:
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise ContextError('Candidate path escapes verification checkout')
            _copy_candidate_path(workspace / relative, checkout / relative)
        actual = git_state(checkout, {
            'path': str(checkout),
            'base_commit': candidate['base_commit'],
        })
        if (actual['tracked_content_sha256'] != candidate['tracked_content_sha256']
                or actual['files'] != candidate['files']):
            raise ContextError('Disposable verification source differs from managed candidate')
        # Template tests expect a sibling checkout. Give them a disposable,
        # independently cloned fixture, never the user's mutable sibling.
        template_head = None
        if record.get('control_repo'):
            template = Path(record['control_repo']).resolve().parent / 'go-project-template'
            if (template / '.go').is_dir():
                if git(template, 'status', '--porcelain', '--untracked-files=all').stdout.strip():
                    raise ContextError('Public template fixture is dirty; refusing partial verification')
                template_head = git_text(template, 'rev-parse', 'HEAD')
                git(workspace, 'clone', '--quiet', '--no-local', str(template), str(checkout.parent / 'go-project-template'))
                fixture = checkout.parent / 'go-project-template'
                if git_text(fixture, 'rev-parse', 'HEAD') != template_head or git(fixture, 'status', '--porcelain').stdout.strip():
                    raise ContextError('Disposable template fixture differs from clean source')
        proof = {
            'schema': 'go-workflow.verification-source.v1',
            'template_head': template_head,
            'candidate_digest': json_hash(candidate_code),
            'tracked_content_sha256': candidate['tracked_content_sha256'],
            'changed_files_sha256': json_hash(candidate['files']),
            'changed_files': sorted(candidate['files']),
            'isolation': 'unregistered_disposable_checkout',
        }
        yield checkout, proof


def canonical_sources(root: Path, task: dict) -> list[Path]:
    sources = [root / name for name in ('project.json', 'vision.json', 'architecture-principles.json', 'hierarchy.json')]
    sources.append(root / 'tasks/active' / (task['id'] + '.json'))
    sources.append(root / 'decisions/events.jsonl')
    sources.append(root / 'runs' / task['id'] / 'model-selection.json')
    for scope in (task.get('architecture') or {}).get('scope_refs', []):
        sources.append(root / 'architecture/briefs' / (scope + '.json'))
    return sources


def selected_files(workspace: Path, task: dict, phase: str) -> list[dict]:
    """Load explicit repo-local context, not the whole scope or global skill tree."""
    selected = []
    if (workspace / 'AGENTS.md').is_file(): selected.append({'kind': 'instructions', 'path': 'AGENTS.md'})
    for field, kind in (('context_files', 'reference'), ('skill_files', 'skill')):
        value = task.get(field, [])
        if isinstance(value, dict): value = value.get(phase, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ContextError(field + ' must be explicit paths or a phase-to-paths map')
        selected.extend({'kind': kind, 'path': item} for item in value)
    if task.get('notepad_path'):
        if not isinstance(task['notepad_path'], str): raise ContextError('notepad_path must be an explicit path')
        selected.append({'kind': 'supplementary_notepad', 'path': task['notepad_path']})
    for item in selected:
        relative = Path(item['path'])
        path = (workspace / relative).resolve()
        if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(workspace.resolve()):
            raise ContextError('Context reference must stay inside the task workspace')
        item['path'] = str(path)
        item['state'] = file_state(path)
        if item['state']['kind'] != 'file': raise ContextError('Selected context file is unavailable: ' + str(path))
    return selected


def feedback_references(value: Any, root: Path) -> list[dict]:
    refs = {}
    def collect(item):
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not isinstance(item.get('sha256'), str):
            raise ContextError('Invalid phase feedback reference')
        path = Path(item['path']).resolve()
        if not path.is_relative_to(root / 'runs') and not path.is_relative_to(root / 'evidence'):
            raise ContextError('Phase feedback reference must be canonical run/evidence data')
        info = file_state(path)
        if info.get('sha256') != item['sha256']: raise ContextError('Phase feedback evidence hash mismatch')
        refs[str(path)] = {'path': str(path), 'sha256': item['sha256']}
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {'context_ref', 'process_result_ref'}: collect(child)
                elif key == 'evidence_refs':
                    if not isinstance(child, list): raise ContextError('evidence_refs must be a list')
                    for ref in child: collect(ref)
                elif key in {'checks', 'critic', 'build', 'result', 'feedback'}: visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
    visit(value)
    return [refs[key] for key in sorted(refs)]


def create_snapshot(workspace: Path, task: dict, phase: str, attempt: int, strategy: str,
                    context: dict, *, feedback: dict | None = None) -> dict:
    workspace = workspace.resolve()
    record = registered_workspace(workspace)
    if record is None or record['task_id'] != task.get('id'): raise ContextError('Managed workspace task required')
    root = workflow_root(workspace)
    current = active_task(root.parent, task['id'], record['owner'])
    if current != task: raise ContextError('Snapshot task differs from current canonical state')
    if phase not in {'build', 'critic', 'repair'} or type(attempt) is not int or attempt < 1:
        raise ContextError('Valid phase and positive attempt required')
    if not isinstance(strategy, str) or not strategy: raise ContextError('Explicit attempt strategy required')
    if feedback is not None and not isinstance(feedback, dict): raise ContextError('Phase feedback must be an object')
    identity = uuid.uuid4().hex
    directory = root / 'runs' / task['id'] / record['run_id'] / f'{phase}-{attempt:02d}-{identity}'
    directory.mkdir(parents=True, exist_ok=False)
    state = git_state(workspace, record)
    sources = {str(path): file_state(path) for path in canonical_sources(root, task)}
    selected = selected_files(workspace, task, phase)
    # Keep exact raw deltas and new/changed file bytes beside the snapshot.
    # No summary is used as a substitute for source or output evidence.
    patch = git(workspace, 'diff', '--binary', '--no-ext-diff', '--no-textconv', record['base_commit'], '--').stdout
    atomic_write_text(directory / 'changes.patch', patch)
    raw = {'diff': {'path': str(directory / 'changes.patch'), 'sha256': digest(patch.encode())}, 'files': {}}
    for name, info in state['files'].items():
        if info['kind'] == 'missing': continue
        source = workspace / name
        content = os.readlink(source).encode() if info['kind'] == 'symlink' else source.read_bytes()
        if digest(content) != info['sha256']: raise ContextError('Workspace changed while capturing context')
        blob = directory / ('blob-' + info['sha256'])
        if not blob.exists():
            with blob.open('xb') as handle: handle.write(content)
        raw['files'][name] = {'path': str(blob), 'sha256': info['sha256']}
    project = json.loads((root / 'project.json').read_text())
    phase_profile = (task.get('execution_contract') or {}).get('phase_profile')
    profiles = project.get('phase_profiles', {}).get(phase_profile, [])
    selected_phase = next((item for item in profiles if item.get('id') == phase), None)
    context = {**context, 'task': current, 'project': project,
               'vision': json.loads((root / 'vision.json').read_text()),
               'architecture_principles': json.loads((root / 'architecture-principles.json').read_text()),
               'hierarchy': json.loads((root / 'hierarchy.json').read_text())}
    snapshot = {'schema': SCHEMA, 'snapshot_id': identity, 'task_id': task['id'], 'phase': phase,
                'attempt': attempt, 'strategy': strategy, 'workspace': record, 'git': state,
                'contract_sha256': json_hash(task.get('execution_contract')), 'task_sha256': json_hash(task),
                'canonical_sources': sources, 'selected_files': selected, 'phase_contract': selected_phase,
                'context': context, 'feedback': feedback or {},
                'feedback_refs': feedback_references(feedback or {}, root), 'raw_evidence': raw,
                'remaining_outcomes': [item for item in task.get('requested_outcomes', []) if item.get('status') != 'verified'],
                'release_receipt': task.get('release_receipt'),
                'notepad_policy': 'Supplementary explanation; cannot override task, code, phase or raw evidence'}
    if len(json.dumps(snapshot).encode()) > MAX_FILE_BYTES:
        raise ContextError('Context snapshot exceeds explicit 16 MiB limit; reduce selected inputs')
    path = directory / 'context.json'
    atomic_json(path, snapshot)
    ref = {'path': str(path), 'sha256': digest(path.read_bytes()), 'snapshot_id': identity}
    verify_snapshot(workspace, task['id'], ref['path'], ref['sha256'])
    return ref


def verify_snapshot(workspace: Path, task_id: str, path: str, sha256: str) -> dict:
    if not isinstance(task_id, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*', task_id):
        raise ContextError('Invalid context task identity')
    if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256): raise ContextError('Invalid context hash')
    workspace = workspace.resolve()
    root = workflow_root(workspace)
    target = Path(path).resolve()
    if not target.is_relative_to(root / 'runs' / task_id): raise ContextError('Context path is outside the canonical task run')
    try:
        data = target.read_bytes()
        if digest(data) != sha256: raise ContextError('Context hash mismatch')
        snapshot = json.loads(data)
        if not isinstance(snapshot, dict) or snapshot.get('schema') != SCHEMA or snapshot.get('task_id') != task_id:
            raise ContextError('Context schema/task mismatch')
        if (snapshot.get('phase') not in {'build', 'critic', 'repair'} or type(snapshot.get('attempt')) is not int
                or snapshot['attempt'] < 1 or not re.fullmatch('[0-9a-f]{32}', str(snapshot.get('snapshot_id')))):
            raise ContextError('Invalid context phase/attempt identity')
        record = registered_workspace(workspace)
        expected_path = root / 'runs' / task_id / record['run_id'] / f"{snapshot['phase']}-{snapshot['attempt']:02d}-{snapshot['snapshot_id']}" / 'context.json'
        if target != expected_path: raise ContextError('Context path does not match run/phase/attempt identity')
        if record != snapshot['workspace']: raise ContextError('Context workspace identity/state changed')
        task = active_task(root.parent, task_id, record['owner'])
        if json_hash(task) != snapshot['task_sha256'] or snapshot['context'].get('task') != task:
            raise ContextError('Canonical task state changed')
        if json_hash(task.get('execution_contract')) != snapshot['contract_sha256']:
            raise ContextError('Canonical execution contract changed')
        if set(snapshot['canonical_sources']) != {str(source) for source in canonical_sources(root, task)}:
            raise ContextError('Canonical context state sources missing or changed')
        if selected_files(workspace, task, snapshot['phase']) != snapshot['selected_files']:
            raise ContextError('Selected context files changed')
        for source, expected in snapshot['canonical_sources'].items():
            source_path = Path(source).resolve()
            if not source_path.is_relative_to(root) or file_state(source_path) != expected:
                raise ContextError('Canonical context state changed: ' + source)
        for name, filename in [('project', 'project.json'), ('vision', 'vision.json'),
                               ('architecture_principles', 'architecture-principles.json'), ('hierarchy', 'hierarchy.json')]:
            if snapshot['context'][name] != json.loads((root / filename).read_text()):
                raise ContextError('Canonical context state changed')
        if git_state(workspace, record) != snapshot['git']: raise ContextError('Git/workspace state changed')
        for item in snapshot['selected_files']:
            source = Path(item['path']).resolve()
            if not source.is_relative_to(workspace) or file_state(source) != item['state']:
                raise ContextError('Selected context file changed')
        if feedback_references(snapshot['feedback'], root) != snapshot['feedback_refs']:
            raise ContextError('Phase feedback references changed')
        raw = snapshot['raw_evidence']
        for item in [raw['diff'], *raw['files'].values()]:
            source = Path(item['path']).resolve()
            if not source.is_relative_to(target.parent) or digest(source.read_bytes()) != item['sha256']:
                raise ContextError('Raw context evidence hash mismatch')
        return snapshot
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ContextError): raise
        raise ContextError('Context unavailable or malformed: ' + str(exc)) from exc
