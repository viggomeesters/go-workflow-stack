# Proportionate task design readiness

Research/intake request, 2026-09-15:

> Moeten wij nog iets aanpassen aan de go-workflow-stack zodat jij taken niet te vrijblijvend oplevert denk je?

Problem: a task may have structurally valid acceptance/verification fields yet leave material behavior, design boundaries and completion proof open to arbitrary interpretation. A recent game-task review required a second pass to specify retargeting, state transitions, visual review and the fact that an unreproduced bug is not fixed. This is an agent authoring problem as well as a potential workflow gap; validation must never be presented as semantic proof.

Existing safeguards already cover material architecture decisions and tracked outcome evidence (completed require-material-architecture-decisions and advice-model-04-outcomes). Extend or reuse them; do not add parallel approval systems or require an ADR for every UI change.

Proposed contract: each substantial task distinguishes accepted decisions, implementation proposals, bounded delegated tuning choices and unresolved material decisions with owner and resolution gate. Describe observable before/after behavior, relevant states/transitions and edge cases, concrete verification matched to outcome, dependencies and explicit non-goals. UI/motion/audio work needs appropriate scale/runtime/listening evidence and references when useful. Research tasks may intentionally leave decisions open; they must not be mistaken for implementation-ready work.

Separate structurally verifiable rules from a content-aware author/reviewer assessment. Do not try to prove semantic quality by word count, a keyword list or another ungrounded boolean. Use a compact review disposition (ready, design/research first, blocked on material decision), reasons and source links in the existing lifecycle if possible. Routine reversible styling/timing choices remain delegated and documented; this is not mandatory user confirmation per detail.

Unknown root cause and an unverified requested outcome cannot count as a fixed bug or product completion. Completion evidence must be linked to the original requested outcomes, not merely the preparatory documents produced along the way. Dependency gates cannot rely on prose alone when execution would silently ignore them; choose a compatible enforcement approach.

Evaluate with paired examples: vague vs actionable visual behavior task; unresolved material rule vs delegated numeric tuning; unreproduced bug; small typo fix; research-only exploration; legacy compatible task. A fresh agent should know what is fixed, what can be decided locally, what must be researched first, and what proves done. Preserve lightweight operation, existing repositories, explicit research boundaries and no GitHub Actions.

This intake does not implement or publish a stack change and does not weaken current safeguards. Placement (skill authoring guidance, review stage, schema/runtime support or combination) is an explicit design decision within the future task, justified by the audit. No global skill changes in this conversation.

## Authorized execution — 2026-09-15

User authorized this existing task. The earlier intake-only restriction is historical.
Chosen placement: repo-local authoring skill and shared native build/repair/critic instructions for semantic assessment; the existing architecture gate for explicitly declared design dependencies, including none/local impact. No task-schema redesign or semantic score. Existing outcome/lifecycle checks remain authoritative.

Accepted delta: `explicit-task-decisions-govern-readiness-v1`; keep the existing material-decision policy. Paired fixtures separate reviewer assessment from deterministic contract checks. Tests cover unchanged trivial/research/legacy behavior, unresolved/delegated rules, invalidated decisions at finish and pending bug outcomes.

## Completed — 2026-09-15

Implemented and released as v0.3.33 (c72c7339f66861fc1c69a80e22514829a46cef09). The task is done and review-approved; all five requested outcomes are verified.

Explicit governing references now gate none/local tasks and legacy autonomous execution before build; the existing finish gate rechecks invalidated decisions. Shared author/critic guidance and eight reasoned examples distinguish structural validity from content readiness. No new schema or approval system.

Verification: 66 focused regressions; macOS and Linux make checks; final tagged Linux gate with 156 compatibility tests, 197 smoke tests, template v0.3.17 pairing and standalone installation. Annotated remote tag and published GitHub Release were read back. Raw results and same-agent critic are in `.go/evidence/task-design-readiness-*` and `.go/runs/task-design-readiness/completion/`. Native transport uses a deterministic CLI double; no live model quality claim is made.

The owned worktree was cleaned up. The automatic workflow-v1 HTML/manifest were validated and their hash checked. Their restricted disclosure classification and blocked publication scan are preserved: these generated files remain local and are not added to the public Git remote. An exact repository-local Git exclude rule protects this directory and keeps it from blocking subsequent tasks. The associated delivery event retains their provenance. The template required no product change for this task.
