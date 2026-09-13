"""Explicit phase model selection and capability checks for controlled adapters."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .execution_contracts import validate_execution_contract


def phase_model(task: dict[str, Any], phase: str) -> dict[str, str] | None:
    if not isinstance(task, dict): raise ValueError('Task must be an object')
    contract = task.get('execution_contract')
    if contract is None:
        return None
    errors = validate_execution_contract(contract)
    if errors:
        raise ValueError('; '.join(errors))
    key = 'critic_model' if phase == 'critic' and 'critic_model' in contract else 'model'
    return deepcopy(contract[key])


def adapter_capabilities(agent: str) -> dict[str, Any]:
    """Opaque adapters cannot promise that a requested model controls execution."""
    return {'adapter': agent, 'model_control': agent == 'codex',
            'capability_source': 'codex app-server model/list' if agent == 'codex' else 'unsupported',
            'reason': None if agent == 'codex' else 'Hermes/custom model control is not implemented; legacy execution remains available'}


def codex_model_catalog(binary: str, repo, timeout_seconds: float = 20) -> list[dict[str, Any]]:
    import json
    import queue
    import subprocess
    import threading
    import time

    inbox = queue.Queue()
    process = subprocess.Popen([binary, 'app-server', '--stdio'], cwd=repo,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    def reader():
        try:
            for line in process.stdout:
                inbox.put(json.loads(line))
        except (ValueError, OSError) as exc:
            inbox.put(exc)
        finally:
            inbox.put(None)
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_seconds
    def send(data):
        process.stdin.write(json.dumps(data) + '\n')
        process.stdin.flush()
    def response(identifier):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise ValueError('Codex capability discovery timed out')
            try: data = inbox.get(timeout=remaining)
            except queue.Empty: raise ValueError('Codex capability discovery timed out') from None
            if not isinstance(data, dict): raise ValueError('Codex capability response unavailable or malformed')
            if data.get('id') != identifier: continue
            if 'error' in data: raise ValueError('Codex capability request rejected')
            result = data.get('result')
            if not isinstance(result, dict): raise ValueError('Codex capability result must be an object')
            return result
    try:
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'go-model-capabilities', 'version': '1'}}})
        response(1)
        send({'method': 'initialized', 'params': {}})
        catalog, cursor, seen = [], None, set()
        for identifier in range(2, 102):
            send({'id': identifier, 'method': 'model/list', 'params': {'limit': 100, 'includeHidden': False, 'cursor': cursor}})
            page = response(identifier)
            if not isinstance(page.get('data'), list): raise ValueError('Codex model catalog is malformed')
            catalog.extend(page['data'])
            cursor = page.get('nextCursor')
            if cursor is None: return catalog
            if not isinstance(cursor, str) or cursor in seen: raise ValueError('Codex model pagination is invalid')
            seen.add(cursor)
        raise ValueError('Codex model catalog exceeded pagination limit')
    finally:
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        process.stdin.close(); thread.join(timeout=1); process.stdout.close()


def controlled_preflight(repo, task, phase, command, native_protocol):
    import json
    import shlex
    import shutil

    selected = phase_model(task, phase)
    if selected is None: return None
    arguments = shlex.split(command)
    native = native_protocol and arguments[:2] == ['codex', 'exec']
    if not native:
        raise ValueError('Selected model requires controlled Codex execution; Hermes/custom model control is unsupported')
    if any(arg in {';', '&&', '||', '|', '>', '>>', '<'} for arg in arguments):
        raise ValueError('Controlled model execution refuses shell composition')
    expected = {'--model': selected['id'], '--config': 'model_reasoning_effort=' + json.dumps(selected['effort'])}
    for flag, value in expected.items():
        if flag not in arguments or arguments.index(flag) + 1 >= len(arguments) or arguments[arguments.index(flag) + 1] != value:
            raise ValueError('Native command does not enforce the selected model/effort')
    binary = shutil.which('codex')
    if not binary: raise ValueError('Codex executable is unavailable')
    catalog = codex_model_catalog(binary, repo)
    for name in ('build', 'critic', 'repair'):
        profile = phase_model(task, name)
        matches = [item for item in catalog if isinstance(item, dict) and item.get('model') == profile['id']]
        if len(matches) != 1: raise ValueError('Unknown or ambiguous model: ' + profile['id'])
        efforts = matches[0].get('supportedReasoningEfforts')
        if not isinstance(efforts, list) or profile['effort'] not in [entry.get('reasoningEffort') for entry in efforts if isinstance(entry, dict)]:
            raise ValueError('Unsupported reasoning effort for model ' + profile['id'] + ': ' + profile['effort'])
    freeze_selection(repo, task)
    return {'requested': selected, 'effective': None, 'confirmation': 'unconfirmed',
            'reason': 'Runtime arguments enforced; no independent effective-model attestation',
            'capabilities': adapter_capabilities('codex')}


def freeze_selection(repo, task):
    import json
    from .execution_contracts import ID_RE
    from .state_io import atomic_json, repository_lock

    task_id = task.get('id')
    if not isinstance(task_id, str) or not ID_RE.fullmatch(task_id):
        raise ValueError('Model selection requires a valid task id')
    profiles = {phase: phase_model(task, phase) for phase in ('build', 'critic', 'repair')}
    root = repo / '.go'
    for state in ('open', 'active', 'blocked', 'done'):
        stored = root / 'tasks' / state / (task_id + '.json')
        if stored.is_file():
            persisted = json.loads(stored.read_text())
            if profiles != {phase: phase_model(persisted, phase) for phase in profiles}:
                raise ValueError('Persisted task differs from frozen phase selection')
    snapshot = {'schema': 'go-workflow.model-selection.v1', 'task_id': task_id, 'profiles': profiles}
    path = root / 'runs' / task_id / 'model-selection.json'
    with repository_lock(root, 'model-selection-' + task_id):
        if path.exists():
            if json.loads(path.read_text()) != snapshot:
                raise ValueError('Model selection is frozen; explicit run migration is required')
        else:
            atomic_json(path, snapshot)
    return snapshot


def validate_model_selection(value):
    from .execution_contracts import EFFORTS
    if not isinstance(value, dict): return ['model_selection must be an object']
    if set(value) != {'requested', 'effective', 'confirmation', 'reason', 'capabilities'}:
        return ['model_selection has missing or unknown fields']
    errors = []
    profile = value['requested']
    if (not isinstance(profile, dict) or set(profile) != {'id', 'effort'}
            or not isinstance(profile.get('id'), str) or not profile['id'].strip()
            or profile.get('effort') not in EFFORTS):
        errors.append('model_selection requested profile invalid')
    if value['effective'] is not None or value['confirmation'] != 'unconfirmed':
        errors.append('effective model confirmation is not supported by this adapter version')
    if not isinstance(value['reason'], str) or not value['reason'].strip(): errors.append('model_selection reason required')
    if not isinstance(value['capabilities'], dict): errors.append('model_selection capabilities must be an object')
    return errors
