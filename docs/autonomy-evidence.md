# Grounded autonomy evidence

Passing commands prove only what those commands inspect. A task adopts the strict
gate with `behavior_review_version: 1`; this explicit version keeps completed
historical reviews and custom adapters readable. A completion critic must
also compare the current candidate with the original requested outcomes and retain
the inspected bytes that support that judgment. Go therefore carries a fresh
`behavior_review` contract in outcome-tracked critic context.

The contract contains the exact original `R#` text, project goals that govern the
task, and architecture references only when task metadata makes them applicable.
Every carried goal or architecture record includes its source and an applicability
explanation. A tiny local change with no architecture references receives no
architecture ceremony and no automatic demand for screenshots or other media.

## Evidence boundary

A passed outcome records at least one inspected evidence item with all of these
bindings:

- task id and original requirement id;
- current candidate digest and fresh critic-context digest;
- repository-relative source or canonical evidence path plus SHA-256;
- evidence kind, role and a concrete observation.

Supported kinds include source, test result, runtime observation, browser capture,
visual capture and audio capture. The critic chooses the kind based on the behavior.
The runtime never infers semantic success from filenames, keywords, labels or a
green process. `preparation`, `unrelated` and `claim_only` roles cannot support a
passed outcome. A changed file, changed candidate or changed critic context makes
the proof stale.

Preparation remains useful evidence of preparation. It is not delivered behavior.
A screenshot from an earlier candidate, a structural test unrelated to the request,
or an author's unreproduced fix claim must leave the affected R# blocked.
Good and bad machine-readable examples live in
[`fixtures/autonomy-outcomes/reviews.json`](../fixtures/autonomy-outcomes/reviews.json).

## Failure and publication

A blocked review names each blocked R# and provides a bounded repair action. Repair
paths must stay inside the original modify scope, checks must come from the original
verification contract, and the acceptance digest must remain unchanged. This makes
critic feedback executable without quietly rewriting the request to manufacture a
green result.

The critic runs before controller-owned publication. It may mark a release-only
outcome `pending_downstream` while `publication_pending` is true. It must not invent
or demand a future tag, push, release or deployment receipt. Final completion still
requires the existing release and remote-readback gates, so this exception does not
weaken publication proof.

Legacy completed lifecycle artifacts remain readable. Versioned outcome-tracked
critic runs and their current completion captures require the grounded
behavior-review object; historical records are not rewritten. The context may be
carried for older R# tasks so adapters can adopt it, but missing strict-review output
only becomes a completion blocker after the task declares version 1.

## Campaign goal audit

Task completion is an input, not the campaign conclusion. When an explicit
campaign has no eligible work left, the controller now runs the same
`go-workflow.campaign-goal-audit.v1` audit exposed by `campaign audit`. It checks
each frozen goal outcome against its exact task/R# links, current hashed lifecycle
artifacts, required release or live receipts, and applicable architecture
conformance. A historical task without explicit outcome-tracking adoption cannot
be upgraded into fresh proof by interpretation.

The audit records `achieved`, `partial`, `blocked`, or `excluded` per outcome.
Only a complete set of achieved or explicitly excluded outcomes can yield
`goal_verified`; an empty queue, an active vision, or declared success metrics do
not. Missing-work proposals are deduplicated and limited to the contract's
research/repair allowances. Broader vision proposals stay outside the run.

The durable JSON audit and Markdown handoff live under
`.go/runs/campaigns/<campaign-id>/`. Their provenance labels keep local
verification, hosted releases, exact live receipts, pending work, and user
decisions visibly distinct.
