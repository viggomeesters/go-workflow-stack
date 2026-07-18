# Changelog

## Unreleased

- Persist exact GO intent text, SHA-256, and an optional durable source reference on intent-created tasks and creation events.
- Track every requested outcome as `verified`, `blocked`, or `rejected` with item-specific evidence.
- Refuse manual and autonomous completion while any tracked outcome remains pending or lacks evidence; regressions prove 7/8 blocks and 8/8 completes.

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
