"""Shared content-review instructions and grounded behavioral proof contracts."""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

AUTHOR_GUIDANCE = (
    "Before implementation, assess task design from the actual task, referenced decisions and evidence. "
    "For substantial work, identify observable before/after behavior, relevant states/transitions and edge cases, "
    "boundaries/non-goals, dependencies and matching proof. Distinguish accepted rules, proposals, "
    "bounded delegated tuning and unresolved material choices with their owner and resolution gate. "
    "Record ready, design_research_first or blocked_on_material_decision with concrete reasons and source/evidence "
    "references in the existing task/run review evidence; the label alone proves nothing. "
    "A schema-valid task, keyword match or long description does not establish semantic readiness. "
    "Use existing architecture decision references and opted-in execution dependencies for real prerequisites; "
    "do not hide blocking dependencies in prose. Research-only work can resolve open choices within its own scope; "
    "do not implement the dependent behavior or silently change a fix request into research completion. "
    "Routine reversible tuning remains delegated within stated bounds; a trivial fix needs only proportionate "
    "behavior and verification, not a new ADR or user approval. If a material implementation choice is unresolved, "
    "report the blocker and research/decision needed before product edits; do not invent the answer."
)

CRITIC_GUIDANCE = (
    "Review semantic adequacy against the original requested outcomes, not just schema validity or successful processes. "
    "Inspect the behavior, relevant state/edge cases, accepted rules, delegated bounds and actual evidence. "
    "Explain blocking gaps with source references and name which R# remains unproven. "
    "An unknown root cause or unreproduced reported bug is not a verified fix; a hypothesis or preparation document "
    "does not satisfy a requested behavior correction. Leave those outcomes pending/blocked through the existing "
    "result protocol rather than declaring success. Research can succeed on research evidence only when that is "
    "the authorized task outcome. Use relevant runtime, visual, motion or listening evidence and references when "
    "the change needs them; do not demand irrelevant media or a new approval for bounded tuning. "
    "Content judgment belongs to the reviewer and must include reasons and inspected evidence; "
    "a readiness label or unverified self-attestation is insufficient."
)


def review_contract():
    return {
        'kind': 'review_instructions_not_verdict',
        'dispositions': ['ready', 'design_research_first', 'blocked_on_material_decision'],
        'evidence_required': 'Concrete reasons and inspected source/evidence references in existing task/run review evidence.',
        'author': AUTHOR_GUIDANCE,
        'critic': CRITIC_GUIDANCE,
    }


def behavior_review_required(task: dict[str, Any]) -> bool:
    """Explicit adoption keeps historical adapters and proof artifacts readable."""
    return task.get('behavior_review_version') == 1


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def behavior_review_context(task: dict[str, Any], vision: dict[str, Any],
                            applicable_architecture: dict[str, Any], candidate_digest: str,
                            *, phase: str) -> dict[str, Any]:
    """Build the exact review input; it is evidence context, never a verdict."""
    goals = []
    for field, label in (('north_star', 'project north star'), ('core_promise', 'project core promise')):
        text = vision.get(field)
        if isinstance(text, str) and text.strip():
            goals.append({'id': field, 'text': text, 'source': f'.go/vision.json#/{field}',
                          'applicability': f'{label} governs every task in project {task.get("project")}'})
    task_text = ' '.join(str(task.get(key) or '') for key in ('summary', 'description'))
    directions = []
    planned = vision.get('planned_direction')
    if isinstance(planned, dict): directions.append(('planned_direction', planned))
    directions.extend((f'proposed_directions/{index}', item)
                      for index, item in enumerate(vision.get('proposed_directions', [])) if isinstance(item, dict))
    for direction_path, direction in directions:
        direction_id = str(direction.get('id') or '')
        plan_ref = str(direction.get('plan_ref') or '')
        if not direction_id or not (direction_id in task_text or (plan_ref and plan_ref in task_text)):
            continue
        target = direction.get('target')
        if isinstance(target, str) and target.strip():
            goals.append({'id': direction_id, 'text': target,
                          'source': f'.go/vision.json#/{direction_path}/target',
                          'applicability': f'task explicitly references direction {direction_id}'})
        for index, metric in enumerate(direction.get('success_metrics', []), 1):
            if isinstance(metric, str) and metric.strip():
                goals.append({'id': f'{direction_id}.success-{index}', 'text': metric,
                              'source': f'.go/vision.json#/{direction_path}/success_metrics/{index - 1}',
                              'applicability': f'task explicitly references direction {direction_id}'})

    metadata = task.get('architecture') if isinstance(task.get('architecture'), dict) else {}
    impact = str(metadata.get('impact') or 'none')
    architecture_refs = []
    if impact in {'material', 'foundational'} or metadata.get('scope_refs') or metadata.get('decision_ids'):
        reason = f'{impact} task declares architecture applicability'
        for principle in applicable_architecture.get('principles', []):
            if isinstance(principle, dict) and principle.get('id'):
                architecture_refs.append({'kind': 'principle', 'id': principle['id'],
                    'source': '.go/architecture-principles.json#/principles/' + str(principle['id']),
                    'value': principle, 'applicability': reason})
        for brief in applicable_architecture.get('briefs', []):
            if isinstance(brief, dict) and brief.get('id'):
                architecture_refs.append({'kind': 'brief', 'id': brief['id'],
                    'source': f'.go/architecture/briefs/{brief["id"]}.json',
                    'value': brief, 'applicability': 'task explicitly references this architecture scope'})
        for decision in applicable_architecture.get('decisions', []):
            payload = decision.get('data') if isinstance(decision, dict) else None
            decision_id = payload.get('decision_id') if isinstance(payload, dict) else None
            if decision_id:
                architecture_refs.append({'kind': 'decision', 'id': decision_id,
                    'source': '.go/decisions/events.jsonl', 'value': decision,
                    'applicability': 'task or applicable brief explicitly references this decision'})

    original_outcomes = [{key: item.get(key) for key in ('id', 'text', 'source')}
                         for item in task.get('requested_outcomes', [])]
    acceptance_digest = _digest(task.get('acceptance', []))
    payload = {
        'schema': 'go-workflow.behavior-review-context.v1',
        'task_id': task.get('id'),
        'phase': phase,
        'candidate_digest': candidate_digest,
        'acceptance_digest': acceptance_digest,
        'product_goals': goals,
        'architecture_refs': architecture_refs,
        'original_outcomes': original_outcomes,
        'evidence_policy': {
            'binding': 'task_requirement_candidate_context_and_inspected_bytes',
            'media': 'only_when_required_by_the_behavior',
            'reject_roles': ['preparation', 'unrelated'],
            'publication': 'downstream receipts are not required by a pre-publication critic',
        },
        'read_only': phase == 'critic',
        'fresh': True,
    }
    payload['context_digest'] = _digest(payload)
    return payload


def _path_in_scope(path: str, task: dict[str, Any]) -> bool:
    clean = path.strip('/')
    for pattern in (task.get('scope') or {}).get('modify', []):
        allowed = str(pattern).strip('/')
        if fnmatch.fnmatchcase(clean, allowed) or clean == allowed or clean.startswith(allowed.rstrip('/') + '/'):
            return True
    return False


def validate_behavior_review(repo: Path, task: dict[str, Any], contract: dict[str, Any],
                             review: Any) -> list[str]:
    """Validate grounding/bindings; semantic adequacy remains the critic's judgment."""
    from .completion import content_snapshot

    errors: list[str] = []
    if not isinstance(review, dict):
        return ['behavior_review must be an object']
    expected = {
        'schema': 'go-workflow.behavior-review.v1',
        'task_id': task.get('id'),
        'context_digest': contract.get('context_digest'),
        'candidate_digest': contract.get('candidate_digest'),
        'acceptance_digest': contract.get('acceptance_digest'),
    }
    for key, value in expected.items():
        if review.get(key) != value: errors.append(f'behavior_review {key} does not match current review context')
    current_digest = content_snapshot(repo, task)['digest']
    if current_digest != contract.get('candidate_digest'):
        errors.append('behavior review candidate is stale')
    status = review.get('status')
    if status not in {'passed', 'blocked'}: errors.append('behavior_review status must be passed or blocked')
    outcomes = review.get('outcomes')
    if not isinstance(outcomes, list):
        outcomes = []
        errors.append('behavior_review outcomes must be a list')
    expected_ids = [str(item.get('id')) for item in task.get('requested_outcomes', [])]
    actual_ids = [str(item.get('requirement_id')) for item in outcomes if isinstance(item, dict)]
    if actual_ids != expected_ids:
        errors.append('behavior_review must cover every original requirement exactly once and in order')
    blocked_ids = set()
    allowed_kinds = {'source', 'test_result', 'runtime_observation', 'browser_capture',
                     'visual_capture', 'audio_capture'}
    allowed_roles = {'behavior_proof', 'preparation', 'unrelated', 'claim_only'}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            errors.append('behavior_review outcome must be an object'); continue
        requirement_id = str(outcome.get('requirement_id') or '')
        outcome_status = outcome.get('status')
        if outcome_status not in {'passed', 'blocked', 'pending_downstream'}:
            errors.append(f'{requirement_id} review status is invalid')
        if outcome_status == 'blocked': blocked_ids.add(requirement_id)
        if not isinstance(outcome.get('rationale'), str) or not outcome['rationale'].strip():
            errors.append(f'{requirement_id} review requires grounded rationale')
        evidence = outcome.get('evidence')
        if not isinstance(evidence, list):
            errors.append(f'{requirement_id} evidence must be a list'); evidence = []
        if outcome_status == 'passed' and not evidence:
            errors.append(f'{requirement_id} passed without inspected behavior evidence')
        if outcome_status == 'pending_downstream' and review.get('publication_pending') is not True:
            errors.append(f'{requirement_id} pending_downstream requires publication_pending')
        release = (task.get('execution_contract') or {}).get('release') or {}
        if outcome_status == 'pending_downstream' and release.get('mode') != 'required':
            errors.append(f'{requirement_id} pending_downstream requires a declared downstream release')
        for item in evidence:
            if not isinstance(item, dict):
                errors.append(f'{requirement_id} evidence must be an object'); continue
            for key, expected_value in (('task_id', task.get('id')), ('requirement_id', requirement_id),
                                        ('candidate_digest', contract.get('candidate_digest')),
                                        ('context_digest', contract.get('context_digest'))):
                if item.get(key) != expected_value: errors.append(f'{requirement_id} evidence {key} binding mismatch')
            if item.get('kind') not in allowed_kinds: errors.append(f'{requirement_id} evidence kind is invalid')
            if item.get('role') not in allowed_roles: errors.append(f'{requirement_id} evidence role is invalid')
            if outcome_status == 'passed' and item.get('role') != 'behavior_proof':
                errors.append(f'{requirement_id} completion uses {item.get("role") or "unsupported"} evidence')
            if not isinstance(item.get('observation'), str) or not item['observation'].strip():
                errors.append(f'{requirement_id} evidence requires an inspected observation')
            path_value = item.get('path')
            if not isinstance(path_value, str) or not path_value:
                errors.append(f'{requirement_id} evidence path is missing'); continue
            path = Path(path_value)
            if path.is_absolute() or '..' in path.parts:
                errors.append(f'{requirement_id} evidence path escapes the repository'); continue
            target = Path(repo).resolve() / path
            if path.parts[0] == '.go':
                from .worktrees import workflow_root
                target = workflow_root(Path(repo).resolve()).parent / path
            try: actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                errors.append(f'{requirement_id} inspected evidence is unavailable'); continue
            if item.get('sha256') != actual_sha:
                errors.append(f'{requirement_id} inspected evidence is stale or hash-mismatched')
    if status == 'passed' and blocked_ids:
        errors.append('passed behavior_review contains blocked outcomes')
    if status == 'blocked' and not blocked_ids:
        errors.append('blocked behavior_review must identify blocked outcomes')
    repairs = review.get('repairs')
    if not isinstance(repairs, list):
        errors.append('behavior_review repairs must be a list'); repairs = []
    repair_ids = set()
    for repair in repairs:
        if not isinstance(repair, dict): errors.append('repair must be an object'); continue
        requirement_id = str(repair.get('requirement_id') or '')
        repair_ids.add(requirement_id)
        if not isinstance(repair.get('action'), str) or not repair['action'].strip():
            errors.append(f'{requirement_id} repair action is required')
        paths = repair.get('paths')
        if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and _path_in_scope(path, task) for path in paths):
            errors.append(f'{requirement_id} repair paths must remain inside original scope')
        checks = repair.get('verification')
        if not isinstance(checks, list) or not checks or not set(checks).issubset(set(task.get('verification', []))):
            errors.append(f'{requirement_id} repair verification must use declared checks')
        if repair.get('acceptance_digest') != contract.get('acceptance_digest'):
            errors.append(f'{requirement_id} repair acceptance binding changed')
    if blocked_ids - repair_ids:
        errors.append('every blocked outcome requires a bounded repair')
    if repair_ids - blocked_ids:
        errors.append('repairs may only target blocked outcomes')
    return errors
