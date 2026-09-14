# Task execution contracts

The optional `execution_contract` adds typed execution intent to a task. Its
schema is `go-workflow.execution-contract.v1`. The runtime implements data validation, intake preservation, dependency
readiness, model selection, owned workspaces and resumable phases. Opted-in
completion requires executed checks, critic evidence and required shipping
readback. Configured publication is available as described below; model identity
remains requested/unconfirmed unless independently observed.

## Selection and compatibility

```json
{
  "schema": "go-workflow.execution-contract.v1",
  "task_kind": "product",
  "model": {"id": "gpt-6-astra", "effort": "high"},
  "release": {"mode": "required", "profile": "project-release"},
  "workspace": {
    "mode": "task_worktree",
    "base_branch": "main",
    "control_state": "repo_local_single_writer"
  }
}
```

`main` and the model above are examples, not hidden defaults. Supported effort
names are a structural vocabulary; the native adapter capability check must
validate the actual model/effort combination before invoking a worker.

Projects can set `execution_defaults` to a complete contract. Task intake merges
defaults with explicit overrides and stores the full result. Within `model`,
`critic_model`, `release` and `workspace`, task fields override project fields;
other fields replace the corresponding value. Subsequent default changes do not
rewrite existing task selections. `critic_model` is optional.

Use `task create --execution-contract profile.json` for a JSON override file.
Execution-brief work units accept `execution_contract` and `dependencies`.
Recommendation storage and promotion retain those work-unit fields. Free-text
intent and spike tasks inherit explicit project defaults; the runtime does not
guess a model from arbitrary prose. Critic follow-ups inherit their parent's
explicit contract and dependencies. JSON export retains the task fields.

The contract is opt-in. Existing tasks without `execution_contract`, including
historical done records and the template's source smoke fixture, are not
rewritten or retroactively required to prove a release. `product` requires
`release.mode: required`. `mechanical`, `no_change` and `smoke` may explicitly
select `mode: none`, but must supply a non-empty policy reason. A no-change
product task is not silently converted into a fake release. Product version,
workflow version, task ID and run ID are separate identities.

## Dependencies

```json
[
  {
    "project": "my-project",
    "task_id": "previous-task",
    "requires": "done_with_required_release_evidence"
  }
]
```

`task create --dependencies dependencies.json` accepts this array. Explicit
dependency intake requires a task contract or project execution defaults.
Historical planning-only dependency metadata remains inert until its task is
explicitly opted in.

The validators reject duplicate references, missing tasks, identity mismatches,
missing participants and cycles before task intake writes. A batch is checked
in full, including forward references, before its first task is written. Local
references use the current project's ID. Cross-project references require an
explicit `dependency_projects` map in project.json, for example
`{"shared-library": "../shared-library"}`. Paths are resolved relative to the
declaring repository; no global vault or inferred sibling queue is consulted.
Each referenced project must in turn declare its own external participants.

`next` and automatic selection omit tasks whose dependencies are not done and,
when work/review fields exist, completed and approved. Claim rechecks this under the task lock.
No eligible tasks is reported as dependency-blocked, not completion. Existing
legacy tasks without a review field retain their historical interpretation.

`done_with_required_release_evidence` additionally requires a bound receipt:

```json
{
  "schema": "go-workflow.release-receipt.v1",
  "task_id": "previous-task",
  "project": "my-project",
  "status": "verified",
  "commit": "0123456789012345678901234567890123456789",
  "evidence": [".go/evidence/previous-task-release.json"]
}
```

Readiness checks receipt identity, shape and references; it does not contact a
remote publisher or authenticate manually authored receipts. The shipping
runtime must create receipts only after independent external readback. Do not
fabricate them to satisfy a dependency. A missing receipt is a blocker.

## Phases and proof

Project `phase_profiles` maps profile names to ordered phase definitions.
An execution contract can reference one through `phase_profile`. Every phase
declares `inputs`, `outputs`, `required_evidence`, `stop_conditions`, `handoff`,
`scope.read`, `scope.modify` and `required_outcomes`, plus schema and ID.
See `schemas/phase-contract.schema.json`. These are data interfaces for later
phase execution, not a requirement to invoke every historical skill.

`verification_evidence` on a task is an array described by
`schemas/verification-evidence.schema.json`. A record identifies the task,
phase and covered requirements. `missing`, `failed`, `not_applicable` and
`passed` are different states. Non-applicability needs a concrete policy
reason. Executed checks identify command, cwd, revision, worktree digest,
exit code and evidence references; `passed` requires exit code zero. A record
for another task is invalid.

Schema/skill lint validates document structure. Test evidence documents command
execution. Release proof establishes the final published revision. They must
not substitute for one another. The shared completion gate invalidates evidence after content changes and
requires matching executed verification, critic and applicable release proof.
Configured publication verifies the prepared candidate before integration.

## Verification

Run `python3 -m pytest tests/test_abc_contracts.py
tests/test_abc_phase_contracts.py -q` and the repository's normal checks.
The tests use temporary repositories and synthetic receipts, without model
calls or external publication. Existing release checks still require Linux
`memfd` support. A successful macOS contract test run does not replace that
release gate.

Legacy provenance and exclusions are recorded in
`.go/plans/legacy-insights.json`. Selected ideas are translated into the current
JSON runtime; historical vault writers and monolithic pipelines are not loaded.

## Configured publication (v0.3.22)

Managed task worktrees can now publish a fully verified candidate. Configure
publication explicitly under the task's named `release_profiles` entry:

```json
{
  "provider": "github-release",
  "remote": "origin",
  "branch": "main",
  "repository": "example/app",
  "publication": {
    "version": {"path": "package.json", "format": "json", "key": "version"},
    "bump": "minor",
    "tag_prefix": "v",
    "changelog": "CHANGELOG.md"
  }
}
```

The alternative version source is `{"path":"VERSION","format":"text"}`.
JSON keys can use an explicit dotted object path. Supported bumps are major,
minor and patch on semantic `X.Y.Z` versions. Both files must be in the task's
modify scope, separate from `.go/` and Git state. A Git-only publisher uses
`provider: git-tag` and omits `repository`. No arbitrary publisher shell command
or deployment hook is inferred.

Start the managed run with its explicit workspace bindings and
`--ship-policy push --allow-push`. The controller freezes profile and authority;
model choice never grants publication permission. Profiles without
`publication` retain `release_pending`. Existing tasks and profiles are not
silently migrated. Publisher mutations run in the canonical checkout; workers
may record their own outcomes but cannot publish through the CLI.

The controller builds, reserves a version against the current remote base and
latest matching release, prepares the version/changelog, then executes every
check and the critic on that final content. Full command output is retained in
content-bound proof files. Workers leave deferred shipping outcomes pending;
blocked/rejected requirements prevent publication. The existing architecture
conformance gate also applies before the first publication effect.

A successful candidate is committed within scope, integrated by fast-forward,
tagged with an annotated tag, atomically pushed with its branch and published
when the profile requires GitHub. Every effect has durable intent before its
write and exact readback afterward. A lost response is reconciled; a conflicting
tag, changed base/profile, unknown readback or live orphan process blocks further
writes. A competing task cannot acquire the integration slot or reuse an active
release-channel reservation. Base reconciliation is explicit: this publisher
does not automatically rebase a changed remote or reuse pre-rebase proof.

`.go/runs/<task>/release-state.json` holds the frozen identity, preparation,
effects and readbacks. Command and time budgets preserve an exact resumable
phase, including publication. The managed resume arguments retain the original
explicit authority. Successful release readback allows finish and approval;
cleanup is a separate checkpoint and never republishes. Local canonical workflow
state is durable between invocations; publishing the product is not a promise
that every changing operational checkpoint has been replicated to another host.
Cross-host transfer and default adoption remain separate tasks.

For a controller that already owns a task workspace, the same driver is exposed
as `release prepare REPO --task-id ID --owner OWNER --run-id RUN
--ship-policy push --allow-push`, `release publish REPO --task-id ID --owner OWNER
--run-id RUN`, and read-only `release status` with those identity arguments.
Standalone publication leaves cleanup to the workspace command. Keep the
canonical state and workspace together for recovery. Do not delete reservations,
overwrite conflicting tags or fabricate observations to bypass a pending run.

Verification commands must leave the owned workspace clean enough to integrate;
Python and pytest caches are redirected/disabled by the controlled collector.
User files and other generated output are preserved and can block cleanup.
GitHub Actions are never consulted. Local bare-Git fixtures exercise the complete
managed lifecycle, while process fixtures cover orphan protection and lost GitHub
responses without contacting an external publisher.


## Deployment and live proof (v0.3.23)

A release profile can explicitly declare no deployment with
`"deployment": {"mode":"none","reason":"Source/library release only"}`.
Omitting this field retains legacy release-only behavior. Neither case claims
that a running service was verified. A required deployment profile must also
configure publication and uses this structure (replace the example target and
adapters during onboarding):

```json
{
  "mode": "required",
  "target": "app-staging",
  "recovery_policy": "resume_only",
  "required_env": ["DEPLOY_TOKEN"],
  "package": {
    "argv": ["python3", "scripts/package.py", "{output}", "{commit}", "{version}"],
    "filename": "app.pkg"
  },
  "deploy": {
    "argv": ["python3", "scripts/deploy.py", "{target}", "{idempotency_key}", "{commit}", "{version}", "{artifact}", "{artifact_sha256}"],
    "idempotency": "required"
  },
  "observe": {
    "argv": ["python3", "scripts/observe.py", "{target}", "{idempotency_key}"],
    "read_only": true
  }
}
```

Add `--allow-deploy` to initial managed execution or `release prepare`, alongside
`--ship-policy push --allow-push`. The controller freezes both authorizations and
the profile; resume retains them. Missing authorization, target, credentials or
recovery policy blocks deployment. Credentials are environment variable names,
not values stored in the profile. There is no inferred rollback or production
migration permission.

Adapters receive structured argv in the canonical checkout under the registered
publication process guard, command/time budget and single publication reservation.
They must leave verified product content unchanged. Supported placeholders are
`{target}`, `{idempotency_key}`, `{commit}`, `{version}`, `{artifact_sha256}`,
`{artifact}` and `{output}`. Package is optional; without it artifact values are
empty argv substitutions and null in observations. Packaging runs after Git release
readback, with the released identity. Output goes into the Git common directory's
`go-workflow-artifacts/<task>/` storage. The controller records its SHA-256 and size;
partial unacknowledged files remain preserved, while a retry uses a fresh path.

The observer must query the target, not echo the requested values. It exits zero
and prints one JSON object using `go-workflow.deployment-observation.v1`, containing
`status` (`absent`, `accepted`, `live` or `unknown`), `authorized`, `available`,
`target`, full `commit`, semantic `version`, `artifact_sha256` (or null), and
`idempotency_key`. Every identity field must match. `live` requires authorized and
available true; absent/accepted require available false. Unauthorized, unknown,
malformed or failed observations are never interpreted as absence or success.
An adapter must authoritatively answer for the queried operation even when it is
absent, and must enforce idempotency for a repeated key. These are trusted adapter
contracts, not provider-independent guarantees made by an exit code.

The durable release state gains a `deploying` phase. It records the same stable
operation key before an external write, observes before each attempt, and leaves
accepted-but-not-live pending without redeploying. A lost response is reconciled
against that operation. Remote release identity is checked again on resume; a
compatible descendant metadata commit is allowed, a conflicting tag is not.

`go-workflow.deployment-evidence.v1` retains executed command references, raw live
observation and package identity. Release evidence links that proof. Completion
validates the profile/version stored in the released Git commit and executes fresh
read-only observation before finish and approval. Historical reporting checks saved
proof without contacting an old target or requiring its old local artifact cache.
The adapters themselves remain trusted infrastructure; no real target is configured
by the stack. Cross-host recovery and adopted project defaults are separate work.


## Adoption and portability (v0.3.24)

Use [explicit lifecycle settings](stack-updates.md#lifecycle-adoption-v0324) to
configure new-project defaults or migrate eligible open task records. Updating
a runtime pin is not policy adoption. Existing named profiles, task overrides,
verification commands, scope and historical proof are preserved; new task coverage
starts pending. Configuration grants no execution, push or deployment permission.

Exported review bundles preserve full contract and provenance fields, including
phase profiles and typed evidence references. They remain review artifacts;
referenced files/Git objects and managed runtime state are not copied or attested.
Historical records without strict completion adoption remain historical, not newly
verified by migration.

## TOML version sources (v0.3.30)

A publication profile can select a static TOML string:

```json
{"path":"pyproject.toml","format":"toml","key":"project.version"}
```

`tool.poetry.version` and other explicit dotted table paths are supported too.
The key must resolve to one unescaped string containing semantic `X.Y.Z`.
Dynamic `project.version`, absent/non-string/pre-release values, duplicate keys,
invalid TOML and ambiguous/unsupported literal representations fail before any
release reservation or preparation write. Dots in key names are path separators;
quoted TOML keys that themselves contain dots are not addressed by this syntax.

The change replaces only the selected version literal. It preserves comments,
whitespace, CRLF, other keys and repeated values elsewhere; it does not reserialize
the TOML document. Parsed before/after meaning verifies the selected occurrence.
Preparation and publication reuse the existing durable intent, scope, proof,
version reservation and lost-response recovery. Profile configuration still
requires separate run authority for push and any deployment. Both project and
lifecycle-settings schemas accept this same version specification.
