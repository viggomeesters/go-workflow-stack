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
