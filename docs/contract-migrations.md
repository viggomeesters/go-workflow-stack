# Contract migrations

`.go/project.json` carries an integer `contract_version`. Missing values are legacy version 1; newly created contracts use the current version.

Migration is review-first and never writes by default:

```bash
python3 cli/go.py migrate . --json
python3 cli/go.py migrate . --apply --json
```

The dry-run returns `go-workflow.migration-plan.v1` with exact paths and operations. `--apply` writes only the proposed project and hierarchy documents, validates the complete `.go` contract, records a migration event, and is idempotent.

Version 2 adds:

- `contract_version: 2`;
- explicit `project_mode` (`project` or `template`);
- immutable `stack_ref` alongside the minimum compatible stack version;
- canonical `epics` in place of legacy `feature_groups`.

A contract newer than the installed stack is rejected instead of being guessed or downgraded.
