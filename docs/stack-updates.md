# Stack pin updates

Project stack changes are explicit and dry-run first. The command resolves a local immutable version tag, verifies that the tagged runtime declares the same semantic version, checks contract compatibility, and shows the proposed project changes without writing:

```bash
./go stack update --to v0.3.8 --json
```

Apply only after reviewing that plan:

```bash
./go stack update --to v0.3.8 --apply --json
```

An applied update atomically replaces `.go/project.json` and writes `.go/updates/<update>.json` first. That record contains the before and after project objects, the resolved stack commit, and rollback status. Missing tags, moving branch names, tag/version mismatches, and runtimes older than the project's contract version fail before project state changes.

The command resolves tags from the local stack checkout by default. Fetch the intended release tag into that checkout first, or pass a different trusted checkout with `--stack-repo`.

`go-workflow doctor` verifies source checkouts against the local annotated tag. For a standalone VCS package installation, it instead requires PEP 610 `direct_url.json` metadata bound to the executing package root and recording Git, the authenticated official repository URL, the exact `v<package-version>` ref, and its resolved 40-character commit. Missing, malformed, archive-based, plaintext-HTTP, unrelated, or mismatched provenance remains unverified; `GO_STACK_ALLOW_DEV=1` is still the only explicit development escape hatch.


## Lifecycle adoption (v0.3.24)

A stack pin update changes `stack_ref` and `required_stack_version` only. Its plan
reports `lifecycle_policy_migrated: false`; it does not select a model, adopt task
policy, claim work or grant publication/deployment authority. Legacy `migrate`
without lifecycle options still performs the separate contract-shape migration.

Inspect existing tasks without changing the repository:

```bash
python3 cli/go.py migrate . --lifecycle --json
```

The result distinguishes existing contracts, open records needing configuration,
and preserved active/blocked/done records. To adopt a policy, prepare a settings
file using `go-workflow.lifecycle-settings.v1`. For example, a source product
using a GitHub release and no deployed service can explicitly configure:

```json
{
  "schema": "go-workflow.lifecycle-settings.v1",
  "execution_defaults": {
    "schema": "go-workflow.execution-contract.v1",
    "task_kind": "product",
    "model": {"id":"gpt-6-astra", "effort":"high"},
    "release": {"mode":"required", "profile":"source-release"},
    "workspace": {"mode":"task_worktree", "control_state":"repo_local_single_writer", "base_branch":"main"}
  },
  "default_verification": ["make check"],
  "release_profiles": {
    "source-release": {
      "provider":"github-release", "remote":"origin", "branch":"main",
      "repository":"example/app",
      "publication": {
        "version":{"path":"VERSION", "format":"text"},
        "bump":"minor", "tag_prefix":"v", "changelog":"CHANGELOG.md"
      },
      "deployment":{"mode":"none", "reason":"This project publishes a source release only"}
    }
  }
}
```

Replace the example model, repository, version source and commands with deliberately
chosen project settings. A required release needs a configured publisher. A real
service separately configures a required deployment profile and its provider adapter.
Ensure declared version/changelog files exist and each product task permits them in
its modify scope. Adoption preserves task scope and existing verification commands;
it does not fabricate a working build or silently widen scope.

```bash
python3 cli/go.py migrate . --lifecycle --config lifecycle.json --json
python3 cli/go.py migrate . --lifecycle --config lifecycle.json --apply --json
# Use the journal path returned by apply; both commands default to a preview.
python3 cli/go.py migrate . --resume .go/migrations/JOURNAL_ID.json --apply --json
python3 cli/go.py migrate . --rollback .go/migrations/JOURNAL_ID.json --apply --json
```

Apply requires a quiescent repository with no active task or live owned worker.
It coordinates existing task/run/integration locks, saves exact before/after JSON
and permissions before mutation, and validates the result. A partial migration
blocks validation and new claims until explicit resume/rollback. Recovery accepts
only the saved before/after states. New, moved or changed tasks prevent rollback;
unknown edits are preserved for reconciliation. Repeating an applied configuration
is a no-op. Journals bind their originating canonical path; active cross-host
transfer is not provided by this command.

Open tasks without a contract adopt the explicit defaults and pending acceptance
coverage. Existing overrides and historical task/evidence bytes remain preserved.
Named release/phase profiles are merged; changing an existing named profile requires
a new name so old tasks retain their contract. Missing settings and claimed open
records are reported as needing configuration. No historical task is reexecuted
and no historical model, release or live proof is invented.

`init`, `adopt` and `spike` accept `--lifecycle-settings lifecycle.json` for a new
project. Existing projects use migration instead. After configuration, ordinary
task creation, intent, execution brief, recommendation promotion, follow-up and
scaffold intake resolve the same model/workspace/release/phase defaults; explicit
work-unit/task overrides remain authoritative. New acceptance coverage is pending,
and follow-up tasks never inherit their parent's completion evidence. Execution
still needs its own run arguments, including separate push/deployment authority.

`bundle export` adds a checksummed `lifecycle_contracts` section with full project
and task records, including source, profiles, phase requirements and evidence
references. `bundle import` validates and preserves it as a review artifact. It
does not insert executable tasks, resume a remote controller or embed referenced
Git objects, external evidence files or active runtime contents. A digest proves
consistency, not a model or reviewer identity. Existing compact bundles remain valid.

Selective legacy provenance remains in `.go/plans/legacy-insights.json`: phase
contracts, requirement completeness, scoped workspaces, explicit handoff and failure
regressions were translated from agent-brain and predecessor viggo-agent-skills.
No old vault writer, mandatory broad skill chain, task-ID versioning, automatic
global skill mutation or GitHub Actions runtime is restored.

## Legacy task metadata compatibility (v0.3.27)

Tasks without `execution_contract` retain their pre-lifecycle metadata semantics.
In particular, string dependency lists and old `verification_evidence` values
remain readable across open, active, blocked and done states. The runtime and
published task schema apply the new strict shapes only after explicit task opt-in;
project defaults do not retroactively opt in stored tasks. Stack updates do not
rewrite task files or manufacture release proof.

A new lifecycle dependency may require a historical task to be done. Its status
and any explicitly required release receipt are still checked, but the runtime
does not traverse that task's legacy dependency metadata as lifecycle edges.
New task intake continues to require explicit structured dependency contracts.

Explicit lifecycle adoption is a separate operation. If an open task's old
metadata does not fit the selected contract, adoption reports the task path and
validation findings before writing any migration document. Resolve those fields
with deliberate task-specific semantics before retrying; do not mass-convert
string references into release requirements or historical output into verified
proof. Historical done records remain byte-preserved during adoption.

## Release compatibility gate

`bash scripts/check-compatibility.sh` runs the bounded legacy/contract/migration
suite in a neutral directory. `scripts/check-linux.sh` invokes it before smoke
tests, so candidate and annotated release checks require both. The matrix uses
synthetic v0.3.7, v0.3.14 and v0.3.26 project pins with all four task states and
checks byte preservation through upgrade and rollback. It does not need private
consumer data or network access to historical runtimes.

`tests/test_release_compatibility_gate.py` is a separate outer test: it executes
the real phase against good source and a disposable deliberately regressed copy.
It must not be placed inside the phase it invokes.

## Exact target compatibility preview (v0.3.29)

The preview materializes the resolved annotated commit and invokes its own
`cli/go.py validate` on a disposable workflow snapshot with the proposed pin.
The full durable `.go` tree is copied; transient lock files are excluded. Explicit
`dependency_projects` participants are also copied (up to 64, cycles deduplicated)
and paths remapped inside the snapshot. Nothing is inferred from sibling folders.
No product code or Git working tree is required for this contract validation.

JSON output includes `compatibility.status`, the exact commit, source digest,
exit code and original-path diagnostics. A failed preview exits nonzero. Apply
repeats this target validation and refuses changed workflow data, moved target
commits or caller-edited plans before writing. It never applies a newer contract
and then tries to approve it using the older calling runtime. A validator that
modifies its snapshot, a linked workflow input, an unavailable entrypoint or a
validation timeout fails closed. Existing guarded update rollback is retained.

Only trusted stack tags should be selected: the subprocess is not a security
sandbox. Snapshot drift checks detect changes by arbitrary editors but cannot
lock external programs. Preview checks contract compatibility, not consumer
product tests or a release gate. Pin updates still adopt no lifecycle policy.

## Guided configuration (v0.3.31)

`Go` remains the public entrypoint. Its internal onboarding planner can inspect a
new project or copied starter without writing `.go`, running checks, choosing a
model or contacting a remote:

```bash
./go onboarding plan . --json
./go onboarding plan . --answers onboarding-answers.json --json
```

It detects `VERSION`, JSON `package.json:version`, TOML `project.version` and
`tool.poetry.version`, the current branch, remote names and candidate `make check`
or `npm test` commands. Detection is a suggestion, not execution or a passing
check. No credentials or remote URLs are included. Missing choices are returned
as `questions`; there is no automatic model, destination or deployment selection.
Agents should reuse already explicit project/user choices and ask only for what
remains unknown. The answers document has this shape (all values are examples):

```json
{
  "model":{"id":"gpt-6-astra","effort":"high"},
  "base_branch":"main",
  "verification":["make check"],
  "publisher":{"provider":"github-release","remote":"origin","repository":"example/app"},
  "version":{"path":"pyproject.toml","format":"toml","key":"project.version"},
  "bump":"minor", "tag_prefix":"v", "changelog":"CHANGELOG.md",
  "deployment":{"mode":"none","reason":"This project publishes source releases only"},
  "task":{"id":"first-release","summary":"Deliver the agreed first product improvement",
          "scope":{"read":["src/**","tests/**"],"modify":["src/**","tests/**"]},
          "acceptance":["The agreed behavior is covered by the project checks and its required release is verified"]}
}
```

Choose an existing static version file; initialize it deliberately first if none
exists. `critic_model` is an optional explicit model/effort override. A `git-tag`
publisher has provider and remote only. Deployment can instead use the existing
fully explicit required adapter profile. Unknown keys, including execution
permission flags, are rejected. `ready` means the configuration and task draft
are valid; model availability, remote setup and actual checks remain execution
preflight responsibilities.

A ready result contains `settings` (`go-workflow.lifecycle-settings.v1`) and
`execution_brief`. Review and save those two JSON objects separately. The draft
includes the selected version/changelog paths in both read and modify scope and
uses the explicit checks and acceptance criteria. It does not claim or run a task.

For a new project, use `adopt --lifecycle-settings settings.json`, then the existing
recommendation/intake path with `execution_brief`. For a copied template, use
`spike --lifecycle-settings settings.json` to replace inherited source state and
then reconcile its tasks with the reviewed brief before execution. Existing
projects use `migrate --lifecycle --config settings.json` instead of replacing
`.go`. The supported Python intake API is `create_tasks_from_execution_brief`;
the CLI equivalent is `recommendation create --brief brief.json` followed by the
normal Go promotion under the user's chosen execution authority. Imported task
outcomes start pending. Configuration and planning never authorize push/deploy.
