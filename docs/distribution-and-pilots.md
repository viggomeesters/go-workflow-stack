# Distribution and pilot evidence

The package exposes a standalone `go-workflow` console command. Install it into an isolated uv tool environment from a trusted release checkout or package source:

```bash
uv tool install --from . go-workflow-stack
go-workflow version --json
go-workflow validate /path/to/project
```

The wheel includes its validation schemas and minimal initialization fixture. The console command therefore does not depend on `../go-workflow-stack`, `../go-project-template`, or the source checkout at runtime. `scripts/check-distribution.sh` proves this by changing to an isolated temporary directory before running `version`, `init`, and `validate`.

`scripts/check-pilots.sh` also reconstructs three synthetic pilots from `fixtures/pilots`: a Python package, a frontend-style TypeScript app, and an existing repository. The recorded metrics in `docs/pilot-metrics.json` require all three to:

- route as valid repo-local projects;
- expose two bounded open tasks;
- preserve the original source file exactly;
- preserve the existing seed commit;
- finish project setup rather than retain template mode.

Run both distribution and pilot checks locally:

```bash
bash scripts/check-pilots.sh
```

Live Hermes evidence is separate. `scripts/run-hermes-acceptance.sh` requires explicit authorization, an executable Hermes binary with version output, two successful real agent tasks, resume completion, and validated native v1 adapter results. It writes `proof.json` only after every assertion succeeds; otherwise it exits with `NOT PROVEN` and no proof artifact.
