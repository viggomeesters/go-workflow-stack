# Proportionate task design readiness

A valid task record does not prove that an implementer knows which behavior to
build or how to prove it. An author and reviewer must assess the actual task and
its sources. The runtime enforces explicit dependencies and evidence boundaries;
it does not recognize good design from keywords, text length or a readiness flag.

## Placement and existing safeguards

| Responsibility | Existing owner | This change |
| --- | --- | --- |
| Task shape, scope and verification fields | `validate_task`, task schema | No new task schema or required fields. Structural validity stays distinct from content review. |
| Design authoring and content review | Repo-local workflow skill; Go handoff; native build/repair/critic context | One shared rubric and grounded review disposition, carried in existing context and phase results. |
| Governing design prerequisites | Architecture briefs and `.go/decisions/events.jsonl` | Explicit scope/decision references must resolve to accepted records at every impact level. |
| Material/foundational controls | Existing architecture claim/conformance gates | Preserve required governing decisions, quality attributes and applicable human authority. Reuse accepted decisions. |
| Research-to-implementation ordering | Opted-in `execution_contract` dependencies | Use actual `dependencies` edges; prose does not enforce ordering. No migration of historical metadata. |
| Requested result and completion | R# outcomes, critic, content-bound verification and release receipts | Preserve the current gates. Review evidence must match the original result, not a substituted preparatory document. |

The audit reproduced two execution defects: explicit local decision references
were skipped by an early return, and the legacy autonomous path could start a
builder before checking architecture prerequisites. Both now reuse the same
architecture claim checks as manual claim. Autonomous preflight reports these
findings; claim rechecks the current record while holding its task lock. Finish
also rechecks a decision that became rejected or superseded after claim.

This is an intentional enforcement change for tasks that already declare explicit
governing references. Unreferenced none/local tasks acquire no new obligation.
Historical done records and their bytes are not rewritten. Material/foundational
rules remain in force. A successful schema check still makes no semantic claim.

## Author and reviewer contract

For substantial work, describe the following in the existing task and relevant
sources. Use a short paragraph when that is sufficient; do not mechanically add
seven headings to every task.

- Observable before/after behavior and the original requested outcomes.
- Relevant states, transitions, interruptions and edge cases.
- Accepted decisions and their source; proposals must remain identified as proposals.
- Bounded delegated choices, such as selecting a duration within an agreed range.
- Unresolved material choices, who can resolve them, and the prerequisite for implementation.
- Scope, non-goals and actual execution dependencies.
- Evidence that can establish each outcome: tests, reproduction, runtime observation,
  visual/motion review, listening or source references as relevant.

Record one concise content assessment in the existing task/run review evidence or
phase result: `ready`, `design_research_first`, or `blocked_on_material_decision`.
Include concrete reasons, inspected source references and the next action. This is
a reviewer disposition, not a new task field that the scheduler treats as proof.
The Go handoff and worker context expose `task_design_review` with
`kind: review_instructions_not_verdict`; no successful semantic status is generated.

For `ready`, explain why the intended behavior and proof are determinate. Do not
claim that behavior is already implemented. For either other disposition, name the
unresolved behavior or evidence gap. The controller repairs the task or creates a
bounded research task before dependent implementation. An owned worker reports
the blocker through its existing result protocol and leaves canonical mutations
to the controller. No new approval is needed for already delegated routine tuning.

## Enforce prerequisites with the existing records

An implementation task whose retargeting rule is unsettled can reference the exact
rule through existing metadata, even when the code change has only local impact:

```json
{"architecture": {"impact": "local", "decision_ids": ["retarget-rule"]}}
```

Until the latest decision record for `retarget-rule` is accepted, manual claim and
autonomous execution refuse dependent work. Missing, proposed, rejected and
superseded records are not accepted. A referenced missing/draft brief also blocks;
the brief's decision references are resolved in the same way. Adding an unrelated
accepted decision does not satisfy an unresolved explicit reference.

A research task may study those alternatives without depending on either proposed
production rule. Put the open choice and owner in its research scope, sources and
acceptance; do not falsely label the unsettled rule as an accepted governing input.
Its completion proves the research deliverable only. To make an implementation
wait for research, explicitly adopt the lifecycle contract and use a dependency:

```json
{"dependencies": [{"project": "motion-demo", "task_id": "research-rule", "requires": "done"}]}
```

`dependencies` are enforced only on tasks with `execution_contract`. Old arbitrary
dependency metadata remains compatible and must never be described as an enforced
edge. Finishing research does not itself accept a design decision: dependent
implementation still needs its referenced governing rule accepted.

## Review examples and proof boundaries

The paired authoring examples and reasoned reviewer dispositions are in
[`tests/fixtures/task-design-readiness.json`](../tests/fixtures/task-design-readiness.json).
Application paths and commands in those examples are illustrative, not runnable
motion tests for this stack.

| Example | Content disposition | Why |
| --- | --- | --- |
| “Make movement smooth” plus whitespace check | Design/research first | Retargeting and interruption rules are absent; the check cannot establish the visual outcome. |
| Current-position retargeting with cancellation/reduced-motion rules | Ready to execute | Observable transitions, boundaries, delegated timing and appropriate proof are specified. |
| Immediate replacement versus queuing remains proposed | Blocked on material decision | Two materially different behaviors remain possible; an explicit decision reference enforces the prerequisite. |
| Fixed retargeting semantics, duration delegated within 120–180 ms | Ready to execute | The implementer can choose and measure the reversible bounded parameter. |
| Reported jump remains unreproduced | Design/research first | A hypothesis and unrelated green checks cannot prove the requested correction. |
| Research-only comparison of the two rules | Ready for research | The authorized deliverable is observations/recommendation; app behavior remains untouched. |
| One documentation typo | Ready to execute | A concrete one-line change and diff inspection suffice. |
| Legacy record without lifecycle adoption | Compatibility preserved | No backfill; assess content and explicitly adopt executable policy for new consequential work. |

Both vague and actionable examples pass structural task validation. The tests
deliberately demonstrate that limitation. The reviewer must read the examples and
their reasons; a test comparing labels would not prove semantic quality.

Bug correction requires evidence that addresses the reported behavior. An unknown
root cause or an unreproduced report cannot be called fixed. Keep its original R#
pending/blocked; record useful research separately. A worker success, passing
schema or preparation document is insufficient. For pre-publication critics,
future tag/push/deployment receipts remain the controller's downstream work; this
rule does not reintroduce a circular demand for release evidence before publication.

Runtime tests exercise actual claim, autonomous preflight, finish refusal, native
context transport and blocked critic handling. The native transport fixture uses
a deterministic CLI double; it proves message delivery and refusal behavior, not
model intelligence or real visual correctness. Same-agent content review and raw
test outputs are retained in this task's repository-local evidence.

Outcome-tracked critic runs now add a fresh `behavior_review` context. Tasks that
declare `behavior_review_version: 1` require a grounded result. Each original R# is bound to the current candidate, critic context
and hashed inspected bytes. This is still not a keyword-based semantic oracle: the
critic supplies the reasoned judgment, while the runtime rejects stale, preparatory,
unrelated or claim-only support and constrains blocked-result repairs. See
[`autonomy-evidence.md`](autonomy-evidence.md) for the proof and publication boundary.
