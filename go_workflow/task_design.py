"""Shared content-review instructions, deliberately not a semantic validator."""

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
