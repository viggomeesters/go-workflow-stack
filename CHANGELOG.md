# Changelog

## 0.3.28 — mandatory compatibility release gate

- Run legacy compatibility, contract and migration checks for every candidate and tagged release.
- Cover three historical pins and all task states with byte-preserving upgrade/rollback assertions.
- Prove an intentionally reintroduced dependency regression blocks the gate.

## 0.3.27 — preserve legacy lifecycle metadata

- Restore the opt-in boundary for dependency and verification metadata in runtime
  validation and the published task schema, preserving historical task bytes.
- Stop lifecycle graph traversal at legacy records while retaining explicit
  predecessor status and required release-receipt checks.
- Identify the task and required metadata resolution before lifecycle adoption
  writes; never infer new dependency or proof semantics.
- Cover legacy shapes across all task states, strict intake, migration and
  v0.3.7 pin upgrade/rollback; pair distribution checks with template v0.3.16.

## 0.3.26 — ordered release campaign and phase-aware critic

- Prove two different task profiles, dependent releases, rejected-critic repair,
  current verification and cleanup through the managed native lifecycle.
- Fix a live-discovered circular critic requirement: pre-publication review checks
  readiness while downstream release/deployment evidence stays pending for the
  controller; final completion still requires that evidence.
- Keep task-worktree outcome writes with the controller; sandboxed workers report
  evidence instead of attempting canonical state mutations.
- Add a reproducible failure/property matrix and separate live runtime receipts,
  preserving failed live evidence and explicit candidate-source attribution.

## 0.3.25 — verified phase worker topology

- Keep fresh ephemeral workers with frozen phase profiles, canonical context and
  one owned task worktree as the supported model-switching baseline.
- Capture versioned local session/turn protocol evidence and real process tests.
- Explicitly defer persistent-session and subagent adapters; distinguish protocol
  capability, fixture control proof and live model confirmation.

## 0.3.24 — explicit lifecycle adoption and preserved intake provenance

- Separate runtime pin updates from explicit project lifecycle configuration.
- Preview, apply, resume and roll back open-task adoption through exact durable
  snapshots, preserving historical tasks, named profiles and user changes.
- Keep model/workspace/release/phase settings and pending acceptance coverage
  across intake and configured scaffolds; preserve full review-bundle provenance.
- Block unfinished migrations and stale stack-update rollback; no execution,
  push, deployment or global skill authority is inferred from configuration.

## 0.3.23 — explicit deployment and live-version proof

- Configure separate deployment authorization, target and idempotent command adapters.
- Bind package bytes and authoritative live commit/version to released content.
- Resume uncertain deployments without replacing their operation identity; preserve
  partial packages, reject wrong live data and recheck before finish/approval.
- Preserve release-only profiles and historical proof in ordinary clones.

## 0.3.22 — resumable configured publication

- Explicit profiles reserve and prepare a scoped semantic version/changelog, then
  require final executed checks, review and architecture proof before shipping.
- Reconcile commit, fast-forward integration, annotated tag, atomic push and
  GitHub publication from durable intent and exact readback after interruption.
- Managed tasks retain budgets and requested model settings through publication,
  finish and separate cleanup; live orphan processes prevent competing writes.
- Distinguish a completed task from a completed queue when blocked work remains.
- Keep unknown remote responses, conflicts and missing authority explicit.
  Existing profiles retain their pending handoff; no deployment or Actions added.

## 0.3.21 — outcome evidence interoperability

- Accept exact proof references from the attributed evidence objects written by
  the standard task outcome CLI. v0.3.20 safely blocked these tracked tasks.
- Exercise real CLI outcome, finish and approval together; arbitrary summaries
  still cannot replace validated lifecycle proof. Published v0.3.20 is retained.

## 0.3.20 — verified lifecycle completion

- Opted-in finish, review approval and completion reporting share content-bound
  verification, critic and required release proof; prose cannot replace checks.
- Capture actual declared commands with raw results and an orphan-worker guard.
  Git-tag and GitHub release readback bind annotated tags and remote branch SHAs.
- Failed opted-in push/readback restores active state. Legacy history remains
  explicitly unmigrated; publication automation remains subsequent work.

## 0.3.19 — managed phase resume

- Resume the same managed task from confirmed build/verification/critic checkpoints with renewed budgets.
- Register worker process groups before dispatch and reject live-owner/orphan takeover, including standalone workspace operations.
- Preserve interrupted setup, requirements, phase evidence and explicit side-effect intent/readback records.
- Repair explicitly moved local checkout pairs, retaining historical proof and invalidating affected checks.
- Leave controlled tasks active at release_pending until the lifecycle publisher supplies verified release evidence.

## 0.3.18 — durable worker context

- Capture immutable managed-native phase context and full raw process evidence.
- Verify canonical state, Git/index/tracked bytes and evidence before worker launch.
- Pass current repair feedback through bounded file references; retain legacy adapters.
- Preserve distinct attempt/critic artifacts and select only explicit phase context.
- Refuse staging/integration/cleanup when index flags can hide user changes.

## 0.3.17 — owned task workspaces

- Add explicit task worktree creation, recovery and claim-following ownership.
- Resolve worker workflow state to one canonical .go and share Git-common locks.
- Check complete task scope before staging; expose held integration slots.
- Preserve dirty or unproven work at cleanup, with recoverable removal failures.
- Keep fresh-worker/resume and publisher orchestration in subsequent ABC tasks.

## 0.3.16

- Enforce explicit model/effort arguments for native Codex phases after installed-runtime capability checks.
- Freeze phase profiles, reject unsupported Hermes/custom model control before build, and preserve legacy execution.
- Record requested/unconfirmed model attribution, native per-turn token counts and measured time without fabricated invoice costs.

## Unreleased

## 0.3.15 — 2026-09-13

- Add opt-in task execution contracts with frozen project/task model settings,
  typed phase/evidence records, explicit cross-project dependencies and
  dependency-aware selection/claim. Preserve contracts through task, intent,
  execution-brief and follow-up intake; leave legacy task records unchanged.
  Model dispatch, worktree execution and publication remain subsequent work.

- Record the exact Git claim base and identity in task state plus its matching
  claim event; `no_diff=true` compares dirty and committed claim-to-HEAD changes,
  requires task/event agreement, and rejects unavailable or non-ancestor bases.
- Bind autonomous ship evidence to the task-delivery commit and an explicit
  same-name branch target resolved from Git's push-remote precedence,
  independent `git ls-remote` readback, and exact remote SHA; mismatches fail
  closed instead of reporting a successful push.

## 0.3.14 - 2026-08-27

- Require every effectively material or foundational task to resolve at least one exact accepted governing decision from task metadata or an applicable brief.
- Apply brief-owned decision and quality-attribute conformance checks only within the owning scope, preventing unrelated multi-scope cross-failures.
- Keep legacy and none/local work compatible while documenting reuse of existing accepted decisions instead of manufacturing per-task ADRs.

## 0.3.13 - 2026-08-26

- Add an opt-in, task-local architecture lane with strict brief and event contracts while keeping legacy repositories valid.
- Resolve exact applicable briefs, decisions, quality attributes, deviations, and waivers into execution context and architecture readback/status commands.
- Fail closed on material architecture claims and finishes until decisions, per-scope conformance, evidence, and required named-human approvals are present.
- Add deterministic impact floors plus explicit classification, review, conformance, and time-bounded waiver commands without creating a second decision or evidence source of truth.

## 0.3.12 - 2026-08-17

- Require a stack-freshness preflight before route, task creation, claim, or product edits in repo-local Go projects.
- Add `stack update --latest` to resolve the highest annotated immutable release and preserve dry-run-first, atomic rollback behavior.
- Make already-current updates true no-ops without project writes, rollback files, or synthetic lifecycle events.

## 0.3.11 - 2026-08-11

- Make restricted shareable delivery a standard approval-time gate for substantial agent tasks, with explicit `required`/`none` overrides, immutable epic versioning, per-epic serialization through autonomous ship/rollback, autonomous-run artifact readback, and fail-closed approval rollback.

## 0.3.10 - 2026-08-11

- Add `go-workflow.delivery.v1` plus `delivery build` for deterministic standalone stakeholder HTML and adjacent SHA-256 provenance manifests.
- Default disclosure to restricted and fail closed before writing public or link-private output containing local paths, credential material, or private-key markers.
- Keep released deliveries immutable, require higher superseding versions for corrections, and expose publication as a separate adapter-gated command.
- Keep runtime delivery validation in fail-closed parity with the published JSON Schema, including strict types, non-empty list items, nullable supersedes, and unknown-property rejection.
- Redesign stakeholder HTML as a compact executive one-pager: one visible summary, three primary outcomes, collapsed proof/provenance, and no raw agent evidence or command output in the human layer.

## 0.3.9 - 2026-08-01

- Classify advice, explicit read-only requests, imperatives, bare Go, and Sent as goal with separate planning and implementation authority.
- Persist compact schema-validated recommendations under repo-local `.go`, with exact SHA-256/source provenance and no copied exploration transcript.
- Promote a pending recommendation into semantic tasks exactly once and continue execution in the same bare-Go invocation after chat context is gone.
- Turn every execution-brief acceptance item into an evidence-backed R# outcome while preserving explicit closure for legacy free-intent outcomes.
- Keep Wayfinder, canonical Go, Hermes links, and the public project template aligned with the durable advice-to-outcome contract.
- Consolidate user-facing repository work behind one `Go` command with plan, task-id, loop-budget, and Wayfinder routing metadata while retaining existing CLI primitives internally.

## 0.3.8 - 2026-07-27

- Persist exact GO intent text, SHA-256, and an optional durable source reference on intent-created tasks and creation events.
- Track every requested outcome as `verified`, `blocked`, or `rejected` with item-specific evidence.
- Refuse manual and autonomous completion while any tracked outcome remains pending or lacks evidence; regressions prove 7/8 blocks and 8/8 completes.
- Add conservative agent-capacity planning: one task defaults to a solo builder plus reviewer lane, while parallel builders require disjoint modify scopes.
- Separate completed work from review disposition with explicit `review`, `approved`, and `needs_fix` lifecycle state; manual finish remains pending while an autonomous passed critic records explicit approval.
- Require attributed finish evidence with changed/no-diff state, verification, runtime, billing mode, ownership, and usage fields.
- Distinguish subscription/free usage from provider invoice cost; API-equivalent estimates are labeled as estimates rather than billed cost.
- Validate the standalone installed distribution during release checks and keep its packaged starter fixture pinned to the release version.
- Document the provenance-first adoption of useful agent-team patterns without copying AGPL implementation code.

## 0.3.7 - 2026-07-18

- Honor `GO_STACK` and explicit `--stack-repo` when an installed runtime resolves immutable tags for `stack update`.
- Refuse to treat wheel/site-packages content as a Git checkout and return an actionable source-checkout error instead.
- Add installed-style regression coverage for template stack updates.

## 0.3.6 - 2026-07-18

- Preserve numbered, plain-numbered, and bulleted GO input as explicit `requested_outcomes` plus `R1`, `R2`, ... acceptance criteria before execution.
- Treat list numbering as traceability rather than an automatic task boundary: bundle coherent outcomes and split only on independent delivery, verification, component/repository, or safe-scope boundaries.
- Generate intent-task scope from actual repository code/config directories so created tasks can safely modify the implementation they describe.
- Repair v1 contract migration by adding the required stack version and linking historical task files before validation.

## 0.3.5 - 2026-07-18

- Fail closed when a repository has no valid local `.go` contract; never route execution to an AW Lite or vault fallback.
- Make every non-empty GO intent create a repo-local task and `task.created` event before autonomous execution, even when backlog tasks already exist.
- Reject direct empty `go-loop --execute` runs with a task-required result instead of claiming false completion.
- Fix `template-check` for standalone `uv tool` package installs by avoiding Git bootstrap into site-packages.
- Add regression coverage for task-first GO loop routing and packaged template execution.

## 0.3.4 - 2026-07-16

- Accept standalone VCS package installations as exact immutable runtimes only when PEP 610 provenance records Git, the required tag, its resolved full commit, and the matching package version.
- Keep source checkouts authoritative and fail closed instead of masking a mismatched checkout with unrelated installed-package metadata.
- Expose the selected runtime identity source and provenance ref through `doctor` for operator readback.

## 0.3.3 - 2026-07-16

- Detect the installed Hermes prompt interface exactly, preferring `-z PROMPT` while retaining explicit legacy `-p PROMPT` support without confusing `-p PROVIDER`.
- Apply the detected prompt capability consistently to native build, critic, and repair phases and fail closed before retry when Hermes is incompatible.
- Expose Hermes compatibility and prompt capability through `agent-check` and `doctor`, with truthful overview-versus-explicit readiness semantics.
- Preserve validated WSL proof from a real two-task Hermes build/critic/resume campaign, including native protocol-result hashes.

## 0.3.2 - 2026-07-16

- Make the doctor mismatch fixture independent of which release tags already exist in the checkout.

## 0.3.1 - 2026-07-15

- Enforce exact commit identity for immutable stack tags across bootstrap and doctor checks.
- Reject release tags that are not annotated or do not dereference to the release commit.
- Require a visible, explicit `GO_STACK_ALLOW_DEV=1` override for unpinned local development runtimes.
- Extract importable routing, task-state, and native-adapter domains behind the CLI facade.
- Require built-in Codex and Hermes phases to emit validated v1 JSON and fail closed on malformed protocol-looking output.
- Add dry-run-first `go stack update` with tag/runtime compatibility checks, atomic project writes, and durable rollback records.
- Make JSON writes and task queue moves atomic, serialize JSONL appends, and add PID-aware process locks that recover dead owners without stealing live locks.
- Ship a standalone `go-workflow` uv-tool entrypoint with packaged schemas/fixture, deterministic Python/frontend/existing-repo pilots, and fail-closed real-Hermes proof artifacts.
- Fall back to an isolated uv runtime when the host has Python 3.11+ but no importable pytest.
- Formalize live Hermes proof as a packaged schema and fail-closed CLI contract with raw-result hash verification and explicit validated copying.
- Require raw doctor/first/resume evidence verification whenever a live Hermes proof is copied for preservation.
- Shell-quote native Codex repository paths and remove unsafe path interpolation from native agent prompts.
- Shell-quote generated router commands and advertise the safe `{repo_shell}` placeholder for custom adapters.
- Prove template bootstrap cannot override its repo-local stack ref through `GO_STACK_REF`.
- Normalize tracked workflow data and schema artifacts to non-executable file modes, with a paired-repository regression gate.

## 0.3.0 - 2026-07-15

- Replace hosted automation with a local Linux/WSL verification command.
- Add immutable `stack_ref` pins and explicit template lifecycle status.
- Add a local-only release preflight that never publishes.
- Split version, migration, and adapter-protocol rules into importable modules.
- Add transactional `.go` contract migrations and the shared versioned Codex/Hermes/custom adapter protocol.

## 0.2.0 - 2026-07-14

- Added bounded autonomous build, verification, deep-critic, repair, transactional ship, and goal-audit loops.
- Added portable cross-machine resume state, Hermes-first executor configuration, WSL doctor checks, and a local Linux verification contract.
- Added project/stack version compatibility plus a live opt-in Hermes acceptance campaign.
- Hardened scope enforcement, dirty-state handling, template pairing, and project-specific template application.

## 0.1.0 - 2026-07-03

- Initial public scaffold for Go Workflow Stack.
- Added public README, MIT license, security/support/contributing docs, issue templates, and local validation.
