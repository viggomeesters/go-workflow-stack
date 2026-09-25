"""Taskwise recovery decisions derived from existing managed phase evidence."""
from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
import re

SCHEMA = 'go-workflow.recovery-plan.v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _semantic(value):
    """Retain failure facts, excluding clocks, worktree hashes and proof filenames."""
    if isinstance(value, dict):
        keys = ('command', 'returncode', 'exit_code', 'status', 'summary', 'stdout', 'stderr',
                'timed_out', 'checks', 'verification', 'completion_check', 'failure')
        return {key: _semantic(value[key]) for key in keys if key in value}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if isinstance(value, str):
        # Generated evidence/workspace identifiers cannot make one failure new.
        value = re.sub(r'(?<=/)[0-9a-f]{32}(?=/|\b)', '<generated-id>', value)
        value = re.sub(r'(?m)^\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?\]?\s*', '<log-time> ', value)
        value = re.sub(r'(?m)^(Elapsed(?: time)?|Duration|Wall time):\s*\d+(?:\.\d+)?\s*(?:s|seconds)\s*$', r'\1: <duration>', value, flags=re.IGNORECASE)
        value = re.sub(r'(\b\d+ (?:failed|passed|skipped|deselected)(?:, \d+ (?:failed|passed|skipped|deselected))* in )\d+(?:\.\d+)?s\b', r'\1<duration>', value)
        value = re.sub(r'/(?:private/)?(?:tmp|var/folders)/[^\s\"\']+', '<temporary-path>', value)
        return value.strip()
    return value


def failure_fingerprint(task_id, result):
    return _digest({'task_id': task_id, 'failure': _semantic(result)})


def recovery_decision(task_id, phase_evidence):
    """A successful repair is activity; only subsequent verification resolves it."""
    methods = ['direct_fix']
    plans = []
    failures = []
    for item in phase_evidence:
        if item.get('phase') in {'recovery_diagnosis', 'recovery_reassessment'}:
            plan = item.get('recovery_plan')
            if isinstance(plan, dict):
                plans.append(plan); methods.append(plan['method']); failures = []
            continue
        result = item.get('result') or {}
        if item.get('phase') == 'critic' and result.get('returncode') == 0:
            failures = []
        if result.get('returncode', 0) != 0 or result.get('status') in {'failure', 'blocked'}:
            if result.get('timed_out') or result.get('status') in {'provider_unavailable', 'rate_limited'}:
                continue
            failures.append(failure_fingerprint(task_id, item.get('failure_result', result)))
    repeated = bool(failures) and failures.count(failures[-1]) >= 2
    return {'required': repeated, 'strategy': methods[-1], 'prior_methods': methods,
            'kind': 'recovery_reassessment' if len(methods) >= 2 else 'recovery_diagnosis',
            'failure_fingerprint': failures[-1] if failures else None,
            'accepted_plans': plans}


def validate_recovery_plan(workspace, value, expected):
    """Verify fresh source-bound diagnosis; no method rename resets evidence."""
    fields = {'schema', 'failure_fingerprint', 'method', 'diagnosis', 'safe_to_continue', 'findings'}
    if not isinstance(value, dict) or set(value) != fields or value.get('schema') != SCHEMA:
        raise ValueError('Recovery requires a versioned recovery_plan')
    if value['failure_fingerprint'] != expected['failure_fingerprint']:
        raise ValueError('Recovery plan is not bound to the current failure')
    if value['safe_to_continue'] is not True:
        raise ValueError('Independent diagnosis found no safe recovery route')
    for key in ('method', 'diagnosis'):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError('Recovery requires a concrete changed method and diagnosis')
    normalized_method = re.sub(r'[^a-z0-9]', '', value['method'].lower())
    if normalized_method in {re.sub(r'[^a-z0-9]', '', method.lower()) for method in expected['prior_methods']}:
        raise ValueError('Recovery cannot repeat the same approach')
    if not isinstance(value['findings'], list) or not value['findings']:
        raise ValueError('Recovery diagnosis requires source-bound findings')
    workspace = Path(workspace).resolve()
    sources = set()
    for finding in value['findings']:
        if not isinstance(finding, dict) or set(finding) != {'path', 'sha256', 'finding'}:
            raise ValueError('Recovery finding requires path, sha256 and finding')
        name = finding['path']
        if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts or Path(name).as_posix() != name:
            raise ValueError('Recovery source must be repository relative')
        if Path(name).parts[0] in {'.go', '.git'}:
            raise ValueError('Recovery findings require product sources, not administrative workflow records')
        scope = expected.get('scope')
        if scope is not None:
            patterns = list(scope.get('read', [])) + list(scope.get('modify', []))
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns if isinstance(pattern, str)):
                raise ValueError('Recovery source is outside the task read/modify scope')
        path = workspace / name
        if not path.resolve().is_relative_to(workspace) or any(p.is_symlink() for p in (path, *path.parents) if p != workspace):
            raise ValueError('Recovery source cannot traverse symlinks')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != finding['sha256']:
            raise ValueError('Recovery source hash is missing or stale')
        if not isinstance(finding['finding'], str) or not finding['finding'].strip():
            raise ValueError('Recovery source requires a substantive finding')
        sources.add((name, finding['sha256']))
    old = {(item['path'], item['sha256']) for previous in expected['accepted_plans'] for item in previous['findings']}
    if sources <= old:
        raise ValueError('Recovery reuses the same source evidence; changed strategy label is insufficient')
    return value
